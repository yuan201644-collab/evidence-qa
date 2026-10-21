#!/usr/bin/env python
"""一键跑分：批量执行 + 三项指标 + 结果落盘。

四项指标（前三项是协作规范第三条要求贴进提交说明的；第四项是口径 3.3 的计数分）：
    1. NL2SQL 执行准确率（子集口径：单表 / 多表，以及 简单/中等/困难 三档）
    2. RAG 答案忠实度
    3. RAG 检索命中率
    4. RAG 单题通过计数（口径 3.3，每题 1 分，对应基础功能 RAG 计数 10 分）
       —— 判据三合一，但**不合成一个布尔**，三个子条件在报告里各自成列

四种预测来源，用来把「跑分」和「谁的代码」解耦：
    oracle   用用例自带的标准 SQL 当预测 —— 判定器与执行链路的自证，**必须 100%**
    jsonl    从文件读别人的预测结果（A/B 先把结果落盘，C 离线跑分）
    http     直接打 A 的工具接口
    dry-run  不执行任何查询，只输出报告骨架 → 这就是 09-23 要交的「空基线」

两种执行方言：
    postgres 容器起来之后用（默认，读 DATABASE_URL）
    sqlite   本机没有 Docker 时用，靠 sqlglot 把 PG 建库脚本转成内存 SQLite

为什么必须有 oracle 模式：
    判定器本身也会错。oracle 模式让「判定器 + 执行链路」自证一次：
    如果标准 SQL 跑自己都拿不到 100%，那说明判定器或执行器有 bug，
    此时任何真实分数都不可信。这条是跑分流程的第一道闸门。

用法：
    python eval/runner.py --predictor oracle                    # 自证，必须 100%
    python eval/runner.py --predictor dry-run                   # 空基线
    python eval/runner.py --suite rag --predictions preds.jsonl # 离线给 B 的预测打分
    python eval/runner.py --predictor http --endpoint http://127.0.0.1:8000/nl2sql
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from metrics import Tally, Verdict, compare_resultsets, gold_sql_has_order_by  # noqa: E402
from rag_metrics import (  # noqa: E402
    RagTally,
    evaluate_faithfulness,
    evaluate_retrieval,
    judge_case,
)

CASES_DIR = ROOT / "eval" / "cases"
REPORTS_DIR = ROOT / "eval" / "reports"
CORPUS_MANIFEST = ROOT / "data" / "corpus" / "manifest.json"


def git_revision() -> tuple[str, bool]:
    """当前代码状态标识：短 SHA + 工作区是否脏。

    口径 `docs/评测口径_v0.1.md` L238 **硬要求**跑分必须带 `git_commit`：
    「分数必须能对应到某一次具体的代码状态，否则『上周 92% 这周 87%』没法排查是改坏了还是运气」。
    M3 之后要连续跑分看趋势，没有这个字段，两次数值差几个百分点时无法归因。

    第二个返回值是工作区是否有未提交改动。口径没要求，但同一 commit 带着未提交改动
    跑出来的分数**不可复现**——排查「改坏了还是运气」时这是第一个要排除的干扰项，
    所以顺手记下来，不额外增加维护成本。

    取不到 git（没装 git、裸源码包、非仓库目录）一律返回 ("unknown", False)：
    评测不允许因为拿不到版本信息就跑不出分。
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=5, check=False,
        )
        if head.returncode != 0:
            return "unknown", False
        sha = head.stdout.strip() or "unknown"

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=5, check=False,
        )
        dirty = status.returncode == 0 and bool(status.stdout.strip())
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


def _load_known_doc_ids_from_db() -> set[str] | None:
    """从向量库 `doc_meta` 取文档全集，作为「引用指向真实文档」的基准。

    连不上、没装驱动、没配 DSN 一律返回 None（让调用方落回清单文件），
    不在判分路径上抛异常 —— 判分能不能跑，不该被一个可选的严格判定拖垮。
    """
    dsn = (os.environ.get("RAG_DSN") or os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        return None
    try:
        import psycopg  # 延迟导入：只用清单的离线场景不必安装/连接
    except ImportError:
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT doc_id FROM doc_meta")
            ids = {str(row[0]) for row in cur.fetchall() if row and row[0]}
    except Exception:  # noqa: BLE001 —— 连不上就落回清单，不干扰判分
        return None
    return ids or None


def load_known_doc_ids() -> set[str] | None:
    """知识库已知文档编号集合（`data/corpus/manifest.json`）。

    用途是判定「引用是否指向**真实存在**的文档」——没有这个基准，
    「引了一份真存在但没检索到的文档」和「引了一份根本不存在的文档（编造）」
    在数据上长得一模一样，后者正是 RAG 幻觉的最强信号，不能放过去。

    编号统一走 `normalize_doc_id` 归一（`KB-001` ↔ `KB-001_xxx.pdf`），
    因为契约的 `doc_id` 与语料文件名是两种写法，见 `eval/README.md`。

    ⚠ 2026-10-18 修：**基准必须跟着语料走**，不能永远只读 Chinook 那份。
    实测事故：政采 14 题引用 `t20170718_8541199.htm` 这类文件名，而本函数读的是
    `data/corpus/manifest.json`（KB-001 系列），两边毫无交集 —— 于是**每一题**的
    `resolved_doc_ids` 都是空、`citation_ok=False`、单题通过 0/14，
    而这些引用其实**全部指向真实存在于政采向量库的文档**。判分基准错配
    被误读成了「系统引用不合格」，差点去改引用生成逻辑。

    取值优先级：
      ① 向量库 `doc_meta` 全集 —— **最贴近「引用是否指向真实文档」的原意**。
         2026-10-18 实测：政采预测引用的 15 份文档全部真实存在于库（2966 份），
         但其中 13 份不在 `gov_corpus_manifest.json` 里（那份清单只登记了
         224 份：24 法规 + 200 公告的 ANN-* 编号），于是判分把这些**真引用**
         判成「解析不到真实文档」，单题通过被压到 2/14。
         用库本身当基准，就是在回答口径原话「指向实际存在的文档」。
         只在设了 `RAG_DSN`/`DATABASE_URL` 且连得上时启用；连不上静默落回 ②③。
      ② 环境变量 `RAG_CORPUS_MANIFEST_PATH` —— 采集侧就是用这个指向政采清单的
         （见 `eval/collect_rag_predictions.py`），判分侧沿用同一份，口径才对得上；
      ③ 兜底 `data/corpus/manifest.json`（Chinook 默认语料）。

    清单缺失时返回 `None`：评测不因为这个文件不在就跑不出分，
    但要如实降级——`judge_case` 会把 `citation_basis` 记成 `none` 并只查片段非空。
    """
    db_ids = _load_known_doc_ids_from_db()
    if db_ids:
        return db_ids

    manifest = CORPUS_MANIFEST
    override = os.environ.get("RAG_CORPUS_MANIFEST_PATH", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            manifest = candidate
    if not manifest.exists():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for key in ("documents", "derived"):
        for d in data.get(key) or []:
            if isinstance(d, dict) and d.get("id"):
                ids.add(str(d["id"]))
    # ⚠ 政采清单的键与 Chinook 不同：只有 `documents`，且每项字段是
    # `{"file": "t20131113_3591938.htm.txt", "title": …, "category": …}`。
    # 向量库 `doc_meta.doc_id` 存的是**去掉 .txt 的文��名**，
    # 契约 DocRef.doc_id 也是这个写法，所以这里必须去掉 .txt 再加进基准，
    # 否则「文档在库里、判分却说它不存在」的错配会再来一次。
    for d in data.get("documents") or []:
        if not isinstance(d, dict):
            continue
        value = d.get("id") or d.get("doc_id") or d.get("file")
        if value:
            text_value = str(value)
            if text_value.endswith(".txt"):
                text_value = text_value[: -len(".txt")]
            ids.add(text_value)
    return ids or None


# ======================================================================================
# 执行器
# ======================================================================================
class Executor:
    dialect = "unknown"

    def execute(self, sql: str) -> tuple[list[tuple], str | None]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class PostgresExecutor(Executor):
    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        import psycopg

        self.conn = psycopg.connect(dsn, connect_timeout=10)

    def execute(self, sql: str) -> tuple[list[tuple], str | None]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
            return rows, None
        except Exception as exc:  # noqa: BLE001 —— 报错本身就是评测要统计的对象
            self.conn.rollback()
            return [], f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        self.conn.close()


class SqliteExecutor(Executor):
    """本机无 Docker 时的降级执行器。用 sqlglot 把 PG 建库脚本转成内存 SQLite。"""

    dialect = "sqlite"

    def __init__(self) -> None:
        sys.path.insert(0, str(ROOT / "eval" / "tools"))
        import sqlglot

        self.sqlglot = sqlglot
        import local_check

        self.conn = local_check.build_sqlite()

    def execute(self, sql: str) -> tuple[list[tuple], str | None]:
        try:
            lite = self.sqlglot.transpile(sql, read="postgres", write="sqlite")[0]
        except Exception as exc:  # noqa: BLE001
            return [], f"方言转换失败: {exc}"
        try:
            return self.conn.execute(lite).fetchall(), None
        except Exception as exc:  # noqa: BLE001
            return [], f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        self.conn.close()


def make_executor(dialect: str, dsn: str | None) -> Executor:
    if dialect == "sqlite":
        return SqliteExecutor()
    if not dsn:
        raise SystemExit(
            "没有数据库连接串。两条路：\n"
            "  1) 起容器后 cp .env.example .env 并填 DATABASE_URL；\n"
            "  2) 本机没有 Docker 时加 --dialect sqlite 走本地内存库。"
        )
    return PostgresExecutor(dsn)


def resolve_dialect(explicit: str | None, dsn: str | None) -> tuple[str, str]:
    """决定执行方言，并说清它是怎么定下来的。

    返回 `(dialect, source)`，`source` 取三种值之一：

    - ``explicit``      调用方给了 ``--dialect``，尊重选择，不再猜。
    - ``env_dsn``       没给 ``--dialect``，但有 ``DATABASE_URL`` / ``--dsn`` → ``postgres``。
    - ``auto_fallback`` 两者都没有 → 回落到内存 SQLite。**这是需要调用方显式告警的情形**：
      判分口径会从「真实 PostgreSQL」悄悄变成「sqlglot 转换后的内存 SQLite」
      （无 pgvector、排序/类型行为不完全等价），而退出码仍是 0。

    抽成纯函数是为了让「回落必须可见」这件事可被单测钉住（见 test_runner_selfcheck.py）。
    """
    if explicit:
        return explicit, "explicit"
    if dsn:
        return "postgres", "env_dsn"
    return "sqlite", "auto_fallback"


AUTO_FALLBACK_WARNING = (
    "[!] 未发现 DATABASE_URL，已自动回落到内存 SQLite 执行。\n"
    "    meta.dialect = sqlite —— 判分口径与 PostgreSQL 不完全等价"
    "（经 sqlglot 方言转换、无 pgvector、排序与类型行为可能有差异），\n"
    "    但退出码仍为 0。要跑真实库请设置 DATABASE_URL 或加 --dsn <连接串>；\n"
    "    若确实要用本地内存库，请显式加 --dialect sqlite，本警告即消失。"
)


# ======================================================================================
# 预测来源
# ======================================================================================
@dataclass
class Prediction:
    """一条预测。三个字段各自服务不同的判定，**不要混用**。

    `retrieved` / `answer_refs` 的拆分理由（A + B 于 2026-09-21 提出，C 于 09-22 落地）：
    B 的 RAG 工具把「全量召回」挂在 `TraceStep.hit` 上、只有 `generate` 挂重排后的 top_k。
    于是从 trace 汇总出的引用是「召回了几条」而不是「答案引用了几条」——
    用它算检索命中率会把「检索对了但模型没引用」也算成没命中，**系统性低估**。
    两个用途拆开：命中率看召回（`retrieved`），引用可核验看答案真正引的那几条（`answer_refs`）。
    """

    sql: str | None = None
    answer: str | None = None
    retrieved: list[dict] = field(default_factory=list)
    """**检索阶段**的全量命中（Top-K）。用于检索命中率，也用作答案引用的兜底。"""

    answer_refs: list[dict] = field(default_factory=list)
    """**答案实际引用**的那几条（契约 `DocRef` 形状）。用于引用校验与忠实度。
    为空时退回 `retrieved` —— 旧预测文件没有这个字段，不能因此判 0。"""

    error: str | None = None
    latency_ms: float | None = None
    clarified: bool | None = None
    """鲁棒性用例专用：系统对信息缺失的输入是否发起了澄清。
    None 表示预测里没给这个信息——此时不能默认算通过，要记成未判定。"""

    def refs_for_citation(self) -> list[dict]:
        """引用类判定该用哪份列表：优先答案实际引用，缺省退回召回结果。"""
        return self.answer_refs or self.retrieved

    def refs_for_retrieval(self) -> list[dict]:
        """检索命中率该用哪份列表：优先**召回**，缺省退回答案引用。

        为什么要有这个兜底（2026-09-22 A 审查报出）：
        拆分前 `retrieved` 的取值是 `row["retrieved"] or row["refs"]`，拆分后去掉了对
        `refs` 的兜底 —— 于是**旧格式预测**（只写契约名 `refs`、不写 `retrieved`）的
        `retrieved` 为空，命中率被 `rag_metrics` 判成「检索结果为空」而**直接归零**。
        引用校验（`refs_for_citation`）仍然对，所以现象很隐蔽：单题通过 1/1、
        命中率 0.0，同一份预测里两个指标互相矛盾。

        兜底为什么用「答案引用」而不是留空：`refs` 在契约里是 `AskResponse.refs`，
        B 的工具按契约名落盘完全合理，不能因此判 0。
        这只是**近似**（答案引用 ⊆ 召回，会低估，不会高估），
        所以只在真召回缺失时启用；有 `retrieved` 时永远以它为准。
        """
        return self.retrieved or self.answer_refs

    @property
    def retrieval_is_approximated(self) -> bool:
        """命中率是否在吃「答案引用」的近似值 —— 报告里要如实标注，不能装作有真召回。"""
        return not self.retrieved and bool(self.answer_refs)


class Predictor:
    name = "base"

    def predict_nl2sql(self, case: dict) -> Prediction:
        raise NotImplementedError

    def predict_robust(self, case: dict) -> Prediction:
        return self.predict_nl2sql(case)

    def predict_rag(self, case: dict) -> Prediction:
        raise NotImplementedError


class OraclePredictor(Predictor):
    name = "oracle"

    def predict_nl2sql(self, case: dict) -> Prediction:
        return Prediction(sql=case["gold_sql"])

    def predict_robust(self, case: dict) -> Prediction:
        if case.get("expected_behavior") == "clarify":
            return Prediction(clarified=True)
        return Prediction(sql=case.get("gold_sql"))

    def predict_rag(self, case: dict) -> Prediction:
        """回放标准答案：答案文本取 `answer_points`，引用转成**契约形状**。

        ⚠️ 这里必须转形状，不能直接把 `gold_refs` 原样当预测喂回去。
        历史缺陷正在于此：oracle 回放的是 `doc`/`anchor` 形状，与判定器的取值字段
        **同形**，于是字段名改错也全绿——自证式全部通过，等于没自证。
        现在回放成契约的 `kind`/`doc_id`/`section_path`/`snippet`，
        判定器一旦只认 `doc`/`anchor`，命中率会立刻掉到 0。这才是有效的回归闸门。

        `doc_id` 故意填**文件名**（`KB-001_xxx.pdf`）而不是归一后的 `KB-001`：
        B 的切片来自文件名，真实预测就是这个形状；让 oracle 也走一遍
        `normalize_doc_id` 归一，归一逻辑坏了才会被发现。
        """
        answer = "；".join(p["point"] for p in case.get("answer_points", []))
        refs = [
            {
                "kind": "doc",
                "doc_id": r.get("doc") or "",
                "section_path": r.get("anchor"),
                "snippet": r.get("snippet") or "",
            }
            for r in (case.get("gold_refs") or [])
        ]
        return Prediction(answer=answer, retrieved=refs)


class DryRunPredictor(Predictor):
    name = "dry-run"

    def predict_nl2sql(self, case: dict) -> Prediction:
        return Prediction(error="dry-run：未接入生成链路")

    def predict_robust(self, case: dict) -> Prediction:
        return Prediction(error="dry-run：未接入澄清交互链路")

    def predict_rag(self, case: dict) -> Prediction:
        return Prediction(error="dry-run：未接入检索与生成链路")


class JsonlPredictor(Predictor):
    name = "jsonl"

    def __init__(self, path: Path) -> None:
        self.by_id: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            self.by_id[str(row["id"])] = row

    def _get(self, case: dict) -> dict:
        row = self.by_id.get(case["id"])
        if row is None:
            return {"error": f"预测文件里没有 {case['id']}"}
        return row

    def predict_nl2sql(self, case: dict) -> Prediction:
        row = self._get(case)
        return Prediction(
            sql=row.get("sql"), error=row.get("error"), latency_ms=row.get("latency_ms")
        )

    def predict_robust(self, case: dict) -> Prediction:
        row = self._get(case)
        return Prediction(
            sql=row.get("sql"),
            error=row.get("error"),
            latency_ms=row.get("latency_ms"),
            clarified=row.get("clarified"),
        )

    def predict_rag(self, case: dict) -> Prediction:
        row = self._get(case)
        # 字段名有三种来源，都要认，别让整份预测因为键名不同判成 0 分：
        #   `retrieved`  —— 本工具的历史叫法，装**检索阶段全量命中**
        #   `refs`       —— 契约 `AskResponse.refs`，历史上两用，现在优先当**答案引用**
        #   `answer_refs`—— 拆分后的显式字段名，最清晰
        # ⚠️ `refs` 只填进 `answer_refs` 即可，**不要再在 `retrieved` 里复制一份**：
        #   `run_rag` 走的是 `pred.refs_for_retrieval()`（`retrieved or answer_refs`），
        #   复制一份会让 `retrieval_is_approximated` 判成 False —— 明明是近似值，
        #   报告里却显示成「真召回」，等于把不可信的分数伪装成可信的。
        return Prediction(
            answer=row.get("answer"),
            retrieved=list(row.get("retrieved") or []),
            answer_refs=list(row.get("answer_refs") or row.get("refs") or []),
            error=row.get("error"),
            latency_ms=row.get("latency_ms"),
        )


class HttpPredictor(Predictor):
    """打 A 的工具接口。接口契约按 ToolResult 走（见 项目概况 四关键接口第 2 条）。"""

    name = "http"

    def __init__(self, endpoint: str, timeout: float = 30.0, kind: str = "nl2sql") -> None:
        import httpx

        self.client = httpx.Client(timeout=timeout)
        self.endpoint = endpoint
        self.kind = kind

    def _post(self, payload: dict) -> Prediction:
        started = time.perf_counter()
        try:
            resp = self.client.post(self.endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return Prediction(error=f"{type(exc).__name__}: {exc}")
        latency = (time.perf_counter() - started) * 1000
        # 兼容两种常见形状：裸字段，或统一包一层 ToolResult
        body = data.get("data", data) if isinstance(data, dict) else {}
        # `refs` 只映射到 `answer_refs`，理由同 `JsonlPredictor.predict_rag`：
        # 别再复制进 `retrieved`，否则近似命中率会被当成真召回。
        return Prediction(
            sql=body.get("sql"),
            answer=body.get("answer"),
            retrieved=list(body.get("retrieved") or []),
            answer_refs=list(body.get("answer_refs") or body.get("refs") or []),
            error=body.get("error"),
            latency_ms=latency,
            clarified=body.get("clarified", body.get("needs_clarification")),
        )

    def predict_nl2sql(self, case: dict) -> Prediction:
        return self._post({"question": case["question"], "database": "chinook"})

    def predict_rag(self, case: dict) -> Prediction:
        return self._post({"question": case["question"], "top_k": 5})


# ======================================================================================
# 跑分
# ======================================================================================
def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_nl2sql(cases: list[dict], executor: Executor | None, predictor: Predictor) -> dict:
    overall = Tally()
    by_difficulty: dict[str, Tally] = {}
    by_scope: dict[str, Tally] = {}
    latencies: list[float] = []
    order_sensitive = Tally()
    """只统计「gold_sql 含 ORDER BY」的用例——它们的行序参与判定。

    单独拉一份出来，是为了报告里能直接回答：「有序用例这一档，我们到底对不对」。
    全量准确率把有序/无序混在一起算，排序写错会被无序用例的通过数稀释掉。
    """

    for case in cases:
        pred = predictor.predict_nl2sql(case)
        # 口径 L88：只有当标准 SQL 含 ORDER BY 时，行的顺序才构成判定项。
        order_matters = gold_sql_has_order_by(case.get("gold_sql") or "")

        if executor is None:  # dry-run：只登记，不执行
            v = compare_resultsets(
                [], [], pred_error=pred.error or "未执行", order_matters=order_matters
            )
            extra = {
                "difficulty": case["difficulty"],
                "scope": case["scope"],
                "order_matters": order_matters,
            }
            overall.add(case["id"], v, extra)
            by_difficulty.setdefault(case["difficulty"], Tally()).add(case["id"], v)
            by_scope.setdefault(case["scope"], Tally()).add(case["id"], v)
            if order_matters:
                order_sensitive.add(case["id"], v)
            continue

        gold_rows, gold_err = executor.execute(case["gold_sql"])
        if pred.sql:
            pred_rows, pred_err = executor.execute(pred.sql)
        else:
            pred_rows, pred_err = [], pred.error or "预测为空"

        if pred.latency_ms:
            latencies.append(pred.latency_ms)

        v = compare_resultsets(
            gold_rows,
            pred_rows,
            gold_error=gold_err,
            pred_error=pred_err,
            order_matters=order_matters,
            # 列语义标注（可选）：只有 "money" 列改走绝对 1 分钱（口径 2.3）。
            # 字段缺席 -> None -> 与历史行为完全一致。
            column_types=case.get("column_types"),
        )
        extra = {
            "difficulty": case["difficulty"],
            "scope": case["scope"],
            "pred_sql": pred.sql,
            "latency_ms": pred.latency_ms,
            "order_matters": order_matters,
            "column_types": case.get("column_types"),
        }
        overall.add(case["id"], v, extra)
        by_difficulty.setdefault(case["difficulty"], Tally()).add(case["id"], v, extra)
        by_scope.setdefault(case["scope"], Tally()).add(case["id"], v, extra)
        if order_matters:
            order_sensitive.add(case["id"], v, extra)

    result = {
        "overall": overall.summary(),
        "by_difficulty": {k: t.summary() for k, t in sorted(by_difficulty.items())},
        "by_scope": {k: t.summary() for k, t in sorted(by_scope.items())},
        "order_sensitive": order_sensitive.summary(),
        "cases": overall.cases,
    }
    if latencies:
        latencies.sort()
        result["latency_ms"] = {
            "n": len(latencies),
            "p50": round(latencies[len(latencies) // 2], 1),
            "p95": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1),
            "max": round(latencies[-1], 1),
        }
    return result


def run_robust(cases: list[dict], executor: Executor | None, predictor: Predictor) -> dict:
    """鲁棒性回归：对应初赛评分表「系统鲁棒性」10 分。

    两类用例，判定方式不同，必须分开统计——混在一起算一个总数，
    会把「答不对」和「该澄清却硬答」两种完全不同的缺陷平均掉：
      expected_behavior=answer  ：退化输入下仍要拿到与标准 SQL 一致的结果
      expected_behavior=clarify ：信息缺失时必须发起澄清；直接生成 SQL 算失败
    """
    answer_tally = Tally()
    clarify_tally = Tally()
    by_degradation: dict[str, Tally] = {}

    for case in cases:
        behavior = case.get("expected_behavior")
        pred = predictor.predict_robust(case)

        if behavior == "clarify":
            if pred.clarified is True:
                v = Verdict("pass", "已发起澄清")
            elif pred.sql:
                v = Verdict("fail", "信息缺失却直接生成了 SQL，未发起澄清")
            elif pred.clarified is None:
                # 关键：缺这个字段不等于通过。默认放行会让「没实现澄清」看起来像通过了。
                v = Verdict("fail", "预测未提供 clarified 字段，无法判定是否澄清（不默认算通过）")
            else:
                v = Verdict("fail", f"未澄清：{pred.error or '系统未要求补充信息'}")
            clarify_tally.add(case["id"], v, {"degradation": case.get("degradation")})
            by_degradation.setdefault(case.get("degradation", "?"), Tally()).add(case["id"], v)
            continue

        # 退化输入子集同样按「gold_sql 是否含 ORDER BY」决定行序是否参与判定（口径 L88）
        order_matters = gold_sql_has_order_by(case.get("gold_sql") or "")
        if executor is None:
            v = Verdict("error", "dry-run：未执行")
        else:
            gold_rows, gold_err = executor.execute(case["gold_sql"])
            if pred.sql:
                pred_rows, pred_err = executor.execute(pred.sql)
            else:
                pred_rows, pred_err = [], pred.error or "预测为空"
            v = compare_resultsets(
                gold_rows,
                pred_rows,
                gold_error=gold_err,
                pred_error=pred_err,
                order_matters=order_matters,
                # 退化输入子集同样按列语义判定（robust 用例当前未标 column_types -> None）
                column_types=case.get("column_types"),
            )
        info = {
            "degradation": case.get("degradation"),
            "canonical_question": case.get("canonical_question"),
            "pred_sql": pred.sql,
            "order_matters": order_matters,
            "column_types": case.get("column_types"),
        }
        answer_tally.add(case["id"], v, info)
        by_degradation.setdefault(case.get("degradation", "?"), Tally()).add(case["id"], v, info)

    def merge(a: Tally, b: Tally) -> dict:
        s = a.summary()
        s2 = b.summary()
        return {
            "total": s["total"] + s2["total"],
            "passed": s["passed"] + s2["passed"],
            "accuracy": round((s["passed"] + s2["passed"]) / (s["total"] + s2["total"]), 4)
            if (s["total"] + s2["total"])
            else 0.0,
        }

    return {
        "overall": merge(answer_tally, clarify_tally),
        "answer_subset": answer_tally.summary(),
        "clarify_subset": clarify_tally.summary(),
        "by_degradation": {k: t.summary() for k, t in sorted(by_degradation.items())},
        "cases": answer_tally.cases + clarify_tally.cases,
    }


def run_rag(cases: list[dict], predictor: Predictor) -> dict:
    """三项一起算：检索命中率 / 答案忠实度 / 单题通过计数（口径 3.3）。

    单题通过是**计数分**（每题 1 分，对应基础功能 RAG 计数 10 分），
    判据是三个子条件同时成立：要点全中 + 禁答实体不出现 + 有合格引用。
    报告里三档明细都要留（`failing`），演示时要能回答「这题为什么不算对」。

    两份引用列表分工不同（2026-09-22 按 A/B 的意见拆开）：
      · 检索命中率 → `pred.refs_for_retrieval()`（优先**召回** `retrieved`）。
        用答案引用算会系统性低估：检索对了但模型没引用，也会算成没命中。
        ⚠️ 兜底必须留着（A 于 2026-09-22 19:05 审查报出）：拆分时若直接写
        `pred.retrieved`，旧格式预测（只写契约名 `refs`）会因 `retrieved` 为空
        被 `evaluate_retrieval` 判成「检索结果为空」而**整题归零**，
        且现象很隐蔽——引用校验仍然通过，同一条预测里命中率 0.0 / 单题通过 1/1 自相矛盾。
        兜底用答案引用只是**近似**（答案引用 ⊆ 召回，只低估不高估），
        所以计数进 `retrieval_approximated`，报告里如实标注。
      · 引用校验 / 单题通过 → `pred.refs_for_citation()`（答案实际引用的那几条）。
        这一项问的是「答案有没有给出可核验的出处」，看召回列表是答非所问。

    「引用指向真实文档」的基准用 `data/corpus/manifest.json` 的文档编号集合。
    这里没有传更严的 `retrieved_doc_ids`：多数预测只有一份 ref 列表，
    拿它当基准会变成「自己跟自己比」的同义反复。
    预测方若另给了 `retrieved`（与 `answer_refs` 不同），自动启用更严口径。
    """
    tally = RagTally()
    known_doc_ids = load_known_doc_ids()
    for case in cases:
        pred = predictor.predict_rag(case)
        answer_refs = pred.refs_for_citation()
        if pred.error and not pred.retrieved and not pred.answer:
            ret = evaluate_retrieval(case, [])
            tally.add(case["id"], ret, None)
            continue
        ret = evaluate_retrieval(case, pred.refs_for_retrieval())
        faith = evaluate_faithfulness(case, pred.answer or "")
        # 只有当 `retrieved` 与答案引用确实不同源时，才把它作为更严的基准传下去；
        # 两者相同时传了等于自己跟自己比，反而不如退回「只查 snippet 非空」。
        strict_basis = (
            pred.retrieved if pred.answer_refs and pred.retrieved != pred.answer_refs else None
        )
        verdict = judge_case(
            case,
            pred.answer or "",
            answer_refs,
            known_doc_ids=known_doc_ids,
            retrieved_doc_ids=(
                {str(r.get("doc_id") or r.get("doc") or "") for r in strict_basis}
                if strict_basis
                else None
            ),
        )
        tally.add(case["id"], ret, faith, verdict, approximated=pred.retrieval_is_approximated)
    return {
        "overall": tally.summary(),
        "failing": tally.failing_cases(),
        "cases": tally.cases,
    }


# ======================================================================================
# 报告
# ======================================================================================
def write_report(payload: dict, tag: str) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = REPORTS_DIR / f"run-{tag}-{stamp}.json"
    md_path = REPORTS_DIR / f"run-{tag}-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def _band_line(bands: dict) -> str:
    parts = []
    for threshold in (90, 80, 70):
        key = f"ge_{threshold}"
        if key in bands:
            mark = "√" if bands[key]["reached"] else "×"
            parts.append(f"{threshold}%:{mark}{bands[key]['score']}分")
    return "  ".join(parts)


def render_markdown(payload: dict) -> str:
    lines = [
        "# 跑分报告",
        "",
        f"- 生成时间（UTC）：{payload['meta']['generated_at']}",
        f"- 代码版本：`{payload['meta'].get('git_commit', 'unknown')}`"
        + (
            "　**⚠️ 工作区有未提交改动，此分数不可复现**"
            if payload["meta"].get("git_dirty")
            else ""
        ),
        f"- 预测来源：`{payload['meta']['predictor']}`",
        f"- 执行方言：`{payload['meta']['dialect']}`",
        f"- 用例文件：{', '.join(payload['meta']['case_files'])}",
        "",
    ]

    if "nl2sql" in payload:
        d = payload["nl2sql"]
        o = d["overall"]
        lines += [
            "## 1. NL2SQL 执行准确率",
            "",
            f"- 全量：**{o['accuracy']:.2%}**（{o['passed']}/{o['total']}）  {_band_line(o['bands'])}",
            f"- 严格口径（不含「仅列序不同」）：{o['strict_accuracy']:.2%}；"
            f"其中仅列序不同被判过 {o['passed_loose_only']} 条（需人工复核）",
            f"- 执行报错：{o['errored']} 条（{o['error_rate']:.2%}）",
        ]
        osens = d.get("order_sensitive")
        if osens and osens["total"]:
            lines.append(
                f"- 其中**有序用例**（`gold_sql` 含 `ORDER BY`，行序参与判定）："
                f"**{osens['accuracy']:.2%}**（{osens['passed']}/{osens['total']}）"
                f"  —— 排序写错只在这一档扣分（评测口径 L88）"
            )
        lines += [
            "",
            "### 按档位",
            "",
            "| 档位 | 用例数 | 通过 | 准确率 | 档位分 |",
            "|---|---|---|---|---|",
        ]
        for diff, s in d["by_difficulty"].items():
            lines.append(
                f"| {diff} | {s['total']} | {s['passed']} | {s['accuracy']:.2%} | {_band_line(s['bands'])} |"
            )
        lines += ["", "### 按范围（对应评分表功能指标 1『基础单表查询』的子集口径）", "",
                  "| 范围 | 用例数 | 通过 | 准确率 | 档位分 |", "|---|---|---|---|---|"]
        for scope, s in d["by_scope"].items():
            lines.append(
                f"| {scope} | {s['total']} | {s['passed']} | {s['accuracy']:.2%} | {_band_line(s['bands'])} |"
            )
        if "latency_ms" in d:
            lat = d["latency_ms"]
            lines += ["", f"### 时延（仅生成侧，样本 {lat['n']}）", "",
                      f"- p50 {lat['p50']} ms / p95 {lat['p95']} ms / max {lat['max']} ms", ""]
        failed = [c for c in d["cases"] if c["kind"] not in ("pass",)]
        if failed:
            lines += ["", "### 未通过明细", "", "| 用例 | 判定 | 原因 |", "|---|---|---|"]
            for c in failed:
                lines.append(f"| {c['id']} | {c['kind']} | {c.get('reason', '')[:80]} |")
            lines.append("")

    if "robust" in payload:
        r = payload["robust"]
        lines += [
            "## 2. 系统鲁棒性回归",
            "",
            f"- 合计：**{r['overall']['accuracy']:.2%}**（{r['overall']['passed']}/{r['overall']['total']}）",
            f"- 退化输入子集：**{r['answer_subset']['accuracy']:.2%}**"
            f"（{r['answer_subset']['passed']}/{r['answer_subset']['total']}）"
            f"  {_band_line(r['answer_subset']['bands'])}",
            f"- 澄清子集：**{r['clarify_subset']['accuracy']:.2%}**"
            f"（{r['clarify_subset']['passed']}/{r['clarify_subset']['total']}）"
            "　—— 信息缺失时是否主动澄清，直接生成 SQL 记失败",
            "",
            "| 退化类型 | 用例数 | 通过 | 准确率 |",
            "|---|---|---|---|",
        ]
        for deg, s in r["by_degradation"].items():
            lines.append(f"| {deg} | {s['total']} | {s['passed']} | {s['accuracy']:.2%} |")
        lines.append("")

    if "rag" in payload:
        r = payload["rag"]["overall"]
        approx = r.get("retrieval_approximated", 0)
        lines += [
            "## 3. RAG 检索命中率 / 答案忠实度 / 单题通过",
            "",
            f"- 检索命中率：**{r['hit_rate']:.2%}**  {_band_line(r['hit_rate_bands'])}",
        ]
        if approx:
            lines.append(
                f"  - ⚠️ 其中 **{approx}/{r['total']}** 题的预测**没给检索结果**"
                f"（只有契约名 `refs`），命中率用的是「答案引用」近似值 —— "
                f"答案引用 ⊆ 召回，**这一档分数被低估，不能当真实检索能力对外报**。"
                f"要拿真值就补 `retrieved` 字段（检索阶段全量 Top-K）。"
            )
        exact = r.get("exact_hits")
        loose = r.get("doc_level_hits")
        if exact is not None and loose is not None:
            lines.append(
                f"  - 严格/宽松并列（**不改变上面的主分**）：精确「文档+块锚点」命中 "
                f"**{exact}/{r['total']} = {r['exact_hit_rate']:.2%}**；"
                f"其余 {loose} 题为文档级宽松命中 —— 只证明命中文档，不证明定位到章节。"
            )
        lines += [
            f"- 答案忠实度：**{r['faithfulness']:.2%}**  {_band_line(r['faithfulness_bands'])}",
            f"- 单题通过（口径 3.3，每题 1 分）：**{r['passed']}/{r['total']} = "
            f"{r['pass_rate']:.2%}**  {_band_line(r['pass_rate_bands'])}",
            f"- 用例数：{r['total']}",
            "",
            "> 单题通过的判据是三个子条件**同时**成立：`gold_points` 全中 + "
            "`forbidden_points` 不出现 + 有合格引用（片段非空且指向真实文档）。",
            "> 三者单独列出，不合成一个布尔——演示时要能回答「这题为什么不算对」。",
            "",
        ]
        failing = payload["rag"].get("failing") or []
        if failing:
            lines += [
                "### 未通过明细（卡在哪个子条件）",
                "",
                "| 用例 | 要点全中 | 无禁答实体 | 引用合格 | 漏掉的要点 | 出现过的禁答实体 | 解析不到文档 | 非空片段数 |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for c in failing:
                lines.append(
                    f"| {c['id']} | {'是' if c['gold_points_all_hit'] else '**否**'} "
                    f"| {'是' if c['forbidden_absent'] else '**否**'} "
                    f"| {'是' if c['citation_ok'] else '**否**'} "
                    f"| {'；'.join(c['missed_points']) or '—'} "
                    f"| {'；'.join(c['hallucinated_entities']) or '—'} "
                    f"| {'；'.join(c['unresolved_doc_ids']) or '—'} "
                    f"| {c['nonempty_snippet_count']} |"
                )
            lines.append("")

    lines += [
        "## 读取提示",
        "",
        "- 本报告由 `eval/runner.py` 自动生成，勿手工改。",
        "- `predictor=oracle` 时全量应恒为 100%；若不是，先修判定器或执行器，再看真实分数。",
        "- 「仅列序不同」被判过的用例已在 JSON 里带 `column_permutation`，人工确认不是巧合。",
        "- RAG 命中率带 `retrieval_approximated` 时，该分数用的是「答案引用」近似召回，"
        "**只低估不高估**，对外报分前先让预测方补 `retrieved`。",
        "- RAG 命中率的「严格/宽松并列」：`exact_hit_rate` 只数精确「文档+块锚点」命中，"
        "`hit_rate` 含文档级宽松兜底 —— **主分数取 `hit_rate`**，"
        "`exact_hit_rate` 用于判断章节定位是否真的对（宽松命中不证明这一点）。",
    ]
    return "\n".join(lines) + "\n"


# ======================================================================================
def _rag_selfcheck(payload: dict, key: str, label: str) -> list[str]:
    """oracle 模式下 RAG 类套件的三项自证（命中率 / 忠实度 / 单题通过）。

    抽成函数是因为**多跳套件漏检过一次**：`--suite multihop` 的 payload 键是
    `multihop` 而不是 `rag`，原先只写 `"rag" in payload` 的地方把它整个跳过了 ——
    多跳 oracle 单题通过只有 80%，却仍被判为自证通过、退出码 0。
    ⚠️ 以后新增任何 RAG 类套件，**必须在这里接线**，否则闸门对它静默失效。

    三项掉分各指向不同地方：
      · 命中率 —— oracle 回放的是契约形状 refs；掉了先查字段名适配有没有回归。
      · 忠实度 / 单题通过 —— 掉分说明**用例自相矛盾**（标准答案自己判不过），
        最常见原因是 `forbidden_entities` 与 `answer_points` 撞了子串
        （中文里「不一致」包含「一致」这类），`validate_cases.py` 会提前拦。
    """
    if key not in payload:
        return []
    r = payload[key]["overall"]
    out: list[str] = []
    if r["hit_rate"] < 1.0:
        out.append(
            f"{label} oracle 检索命中率 {r['hit_rate']:.2%}，未达 100%"
            "（oracle 回放的是契约形状 refs，掉了先查字段名适配有没有回归）"
        )
    if r["faithfulness"] < 1.0:
        out.append(
            f"{label} oracle 忠实度 {r['faithfulness']:.2%}，未达 100%"
            "（说明答案要点与关键词不自洽，标准答案自己都判不过）"
        )
    if r["pass_rate"] < 1.0:
        out.append(
            f"{label} oracle 单题通过 {r['pass_rate']:.2%}，未达 100%"
            "（三个子条件里至少一个有假阴性，看报告「未通过明细」定位）"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="评测跑分")
    ap.add_argument("--suite",
                    choices=["nl2sql", "robust", "rag", "multihop", "all",
                             "gov", "gov_robust", "gov_rag"],
                    default="nl2sql")
    ap.add_argument("--predictor", choices=["oracle", "dry-run", "jsonl", "http"], default="oracle")
    ap.add_argument("--predictions", type=Path, help="--predictor jsonl 时的预测文件")
    ap.add_argument("--endpoint", help="--predictor http 时的接口地址")
    ap.add_argument("--dialect", choices=["postgres", "sqlite"], default=None,
                    help="默认：有 DATABASE_URL 用 postgres，否则 sqlite")
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--tag", default=None, help="报告文件名标签，默认取 predictor 名")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    dialect, dialect_source = resolve_dialect(args.dialect, args.dsn)

    if args.predictor == "jsonl":
        if not args.predictions:
            raise SystemExit("--predictor jsonl 必须给 --predictions <文件>")
        predictor: Predictor = JsonlPredictor(args.predictions)
    elif args.predictor == "http":
        if not args.endpoint:
            raise SystemExit("--predictor http 必须给 --endpoint <URL>")
        predictor = HttpPredictor(args.endpoint)
    elif args.predictor == "dry-run":
        predictor = DryRunPredictor()
    else:
        predictor = OraclePredictor()

    suite = args.suite
    if args.predictor == "http" and suite == "all":
        raise SystemExit("HTTP 预测器一次只打一个接口，请分别用 --suite nl2sql / --suite rag")

    revision, dirty = git_revision()

    case_files: list[str] = []
    payload: dict = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "git_commit": revision,
            "git_dirty": dirty,
            "predictor": predictor.name,
            "dialect": dialect,
            "dialect_source": dialect_source,
            "case_files": case_files,
            "top_k": args.top_k,
        }
    }

    executor: Executor | None = None
    if suite in ("nl2sql", "robust", "all", "gov", "gov_robust") and args.predictor != "dry-run":
        # 只在这一支告警：rag / multihop 等套件根本不建执行器，对它们报「回落」是假阳性。
        if dialect_source == "auto_fallback":
            # 不受 --quiet 抑制：这是「判分口径被悄悄换掉」的告警，不是普通摘要输出。
            print(AUTO_FALLBACK_WARNING, file=sys.stderr)
        executor = make_executor(dialect, args.dsn)

    def load_suite(name: str) -> list[dict]:
        # 政采题集放在子目录里（gov_procurement/），与常驻 Chinook 题集分开，
        # 因为它们连的是另一个库（askdata_gov），分数不可相加、不可互相替代。
        path = CASES_DIR / name
        case_files.append(str(path.relative_to(ROOT)))
        return load_cases(path)

    if suite in ("gov", "gov_robust", "gov_rag"):
        if suite == "gov":
            cases = load_suite("gov_procurement/nl2sql.jsonl")
            payload["gov"] = run_nl2sql(cases, executor, predictor)
            if not args.quiet:
                o = payload["gov"]["overall"]
                print(f"政采问数：{o['passed']}/{o['total']} = {o['accuracy']:.2%}  "
                      f"（报错 {o['errored']}，仅列序不同 {o['passed_loose_only']}）")
        elif suite == "gov_rag":
            # 政采文档单源 14 题（法规 8 + 公告 6）。判据与 chinook 单轮完全同形
            # （gold_refs / answer_points / forbidden_entities），所以直接复用 run_rag，
            # 只是换题集 —— 2026-10-18 之前没有这个分支，拿政采预测跑 `--suite rag`
            # 会去读 chinook 的 rag.jsonl（21 题），分母与题集全对不上。
            cases = load_suite("gov_procurement/rag.jsonl")
            if not cases:
                raise SystemExit("gov_procurement/rag.jsonl 为空")
            payload["gov_rag"] = run_rag(cases, predictor)
            if not args.quiet:
                r = payload["gov_rag"]["overall"]
                print(f"政采文档单源：命中率 {r['hit_rate']:.2%}，"
                      f"忠实度 {r['faithfulness']:.2%}，"
                      f"单题通过 {r['passed']}/{r['total']} = {r['pass_rate']:.2%}")
        else:
            cases = load_suite("gov_procurement/robust_cases.jsonl")
            payload["gov_robust"] = run_robust(cases, executor, predictor)
            if not args.quiet:
                r = payload["gov_robust"]
                print(f"政采鲁棒性：{r['overall']['passed']}/{r['overall']['total']} = "
                      f"{r['overall']['accuracy']:.2%}  "
                      f"（退化输入 {r['answer_subset']['accuracy']:.2%} / "
                      f"澄清 {r['clarify_subset']['accuracy']:.2%}）")

    try:
        if suite in ("nl2sql", "all"):
            cases = load_suite("nl2sql.jsonl")
            if not cases:
                raise SystemExit("nl2sql.jsonl 为空")
            payload["nl2sql"] = run_nl2sql(cases, executor, predictor)
            if not args.quiet:
                o = payload["nl2sql"]["overall"]
                print(f"NL2SQL：{o['passed']}/{o['total']} = {o['accuracy']:.2%}  "
                      f"（报错 {o['errored']}，仅列序不同 {o['passed_loose_only']}）")

        if suite in ("robust", "all"):
            cases = load_suite("robust_cases.jsonl")
            if not cases:
                print("提示：robust_cases.jsonl 还不存在或为空，跳过鲁棒性回归。")
            else:
                payload["robust"] = run_robust(cases, executor, predictor)
                if not args.quiet:
                    r = payload["robust"]
                    print(f"鲁棒性：{r['overall']['passed']}/{r['overall']['total']} = "
                          f"{r['overall']['accuracy']:.2%}  "
                          f"（退化输入 {r['answer_subset']['accuracy']:.2%} / "
                          f"澄清 {r['clarify_subset']['accuracy']:.2%}）")

        if suite in ("rag", "all"):
            cases = load_suite("rag.jsonl")
            if not cases:
                print("提示：rag.jsonl 还不存在或为空，跳过 RAG 跑分。")
            else:
                payload["rag"] = run_rag(cases, predictor)
                if not args.quiet:
                    r = payload["rag"]["overall"]
                    print(
                        f"RAG：命中率 {r['hit_rate']:.2%}，忠实度 {r['faithfulness']:.2%}，"
                        f"单题通过 {r['passed']}/{r['total']} = {r['pass_rate']:.2%}"
                    )
                    approx = r.get("retrieval_approximated", 0)
                    if approx:
                        print(
                            f"   ⚠️ 命中率含近似：{approx}/{r['total']} 题没给 `retrieved`，"
                            "用的是答案引用（低估），不能当真实检索能力对外报"
                        )
                    for c in payload["rag"].get("failing") or []:
                        why = []
                        if not c["gold_points_all_hit"]:
                            why.append(f"漏要点{'、'.join(c['missed_points']) or '?'}")
                        if not c["forbidden_absent"]:
                            why.append(f"出现禁答实体{'、'.join(c['hallucinated_entities'])}")
                        if not c["citation_ok"]:
                            why.append(
                                f"引用不合格（非空片段 {c['nonempty_snippet_count']} 条，"
                                f"解析不到文档 {c['unresolved_doc_ids'] or '无'}）"
                            )
                        print(f"    × {c['id']}：{'；'.join(why)}")

        if suite == "multihop":
            # 多跳专用套件：与 --suite rag 用**同一套评分逻辑**，但用例全部是跨文档/多跳题。
            # 刻意不并入 --suite all：all 是发布回归门，其套件构成不应随实验性能力变化；
            # 否则以后关掉多跳，all 的报告形状也会跟着变。
            cases = load_suite("rag_multihop.jsonl")
            if not cases:
                print("提示：rag_multihop.jsonl 还不存在或为空，跳过多跳跑分。")
            else:
                payload["multihop"] = run_rag(cases, predictor)
                if not args.quiet:
                    r = payload["multihop"]["overall"]
                    print(
                        f"多跳：命中率 {r['hit_rate']:.2%}，忠实度 {r['faithfulness']:.2%}，"
                        f"单题通过 {r['passed']}/{r['total']} = {r['pass_rate']:.2%}"
                    )
                    approx = r.get("retrieval_approximated", 0)
                    if approx:
                        print(
                            f"   ⚠️ 命中率含近似：{approx}/{r['total']} 题没给 `retrieved`，"
                            "用的是答案引用（低估），不能当真实检索能力对外报"
                        )
                    for c in payload["multihop"].get("failing") or []:
                        why = []
                        if not c["gold_points_all_hit"]:
                            why.append(f"漏要点{'、'.join(c['missed_points']) or '?'}")
                        if not c["forbidden_absent"]:
                            why.append(f"出现禁答实体{'、'.join(c['hallucinated_entities'])}")
                        if not c["citation_ok"]:
                            why.append(
                                f"引用不合格（非空片段 {c['nonempty_snippet_count']} 条，"
                                f"解析不到文档 {c['unresolved_doc_ids'] or '无'}）"
                            )
                        print(f"    × {c['id']}：{'；'.join(why)}")
    finally:
        if executor is not None:
            executor.close()

    tag = args.tag or predictor.name
    json_path, md_path = write_report(payload, tag)
    if not args.quiet:
        print(f"\n报告：{json_path.relative_to(ROOT)}")
        print(f"     {md_path.relative_to(ROOT)}")
        if payload["meta"].get("git_dirty"):
            print("   ⚠️ 工作区有未提交改动，本次分数不可复现 —— 先 commit 再跑分")

    # oracle 自证失败必须让调用方拿到非零退出码，否则 CI / 一键脚本会把坏数据当绿的
    bad: list[str] = []
    if args.predictor == "oracle":
        if "nl2sql" in payload:
            o = payload["nl2sql"]["overall"]
            if o["accuracy"] < 1.0:
                bad.append(f"NL2SQL oracle 自证 {o['accuracy']:.2%}，未达 100%")
        bad.extend(_rag_selfcheck(payload, "rag", "RAG"))
        bad.extend(_rag_selfcheck(payload, "multihop", "多跳"))
        if "robust" in payload:
            for key, label in (("answer_subset", "退化输入"), ("clarify_subset", "澄清")):
                sub = payload["robust"][key]
                if sub["total"] and sub["accuracy"] < 1.0:
                    bad.append(f"鲁棒性 oracle {label}子集 {sub['accuracy']:.2%}，未达 100%")
    if bad:
        print("\n[FAIL] oracle 自证未通过：")
        for b in bad:
            print("  -", b)
        print("先修判定器/用例，再看真实分数。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
