"""评测面板的后端支撑：在真实链路上跑评测集，逐题推进度。

为什么要有它（而不是让评委去命令行跑 `eval/runner.py`）：
    赛题初赛「演示效果」与「NL2SQL / RAG 准确率」三项都靠举证。命令行跑分
    对评委是不可见的，而一个能点的按钮 + 一张带目标档位的指标表，把
    「我们的准确率是多少」从口头陈述变成现场可复现的事实。

与 `eval/runner.py` 的分工（**不要把它们合并**）：
    · `eval/runner.py` 是**判分权威**：它自带判定器，口径冻结在 `eval/README.md`，
      支持 oracle / dry-run / jsonl / http 四种预测器，产出的报告要进交付物。
    · 本模块是**演示通道**：不做判定，只负责「把题目逐条发给真实链路、
      把答案与 gold 一起交给 runner / metrics 里那套判定函数」。
      判分逻辑一律复用（`compare_resultsets` / `judge_case` / `gold_sql_has_order_by`），
      **不在这里重新实现** —— 否则同一份用例在报告里和面板上会得出两个数。

为什么逐题推进度而不是一次性返回：
    NL2SQL 15 题、RAG 21 题，真实链路每题数秒，整套跑完要几分钟。界面上必须
    看到「第 N/M 题」在动，否则评委以为卡死了 —— 这正是演示分的一部分。

已知边界（面板上要如实说，不能让人误读）：
    · 这里跑出来的准确率是**演示口径**：一次运行、不重试、不含 runner 的
      `--predictor oracle` 对照。正式报告的绝对值以 runner 产出为准。
    · 判分不通过时给的是人话理由（缺哪个要点、哪个禁答实体出现了），
      这是可解释性的一部分：评委要能问「这题为什么不算对」。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "eval" / "cases"

#: 允许面板跑的套件。键是稳定标识，值是 (用例文件, 中文名, 对应评分表项, 判分方式)。
#: 判分方式只有两种：`sql` 走结果集比对，`rag` 走要点+禁答+引用三条件。
SUITES: dict[str, dict[str, str]] = {
    "nl2sql": {
        "file": "nl2sql.jsonl",
        "title": "NL2SQL 执行准确率",
        "scoring": "初赛功能指标 1（10 分）/ 决赛性能指标 1（10 分）",
        "judge": "sql",
    },
    "rag": {
        "file": "rag.jsonl",
        "title": "RAG 答案忠实度",
        "scoring": "初赛功能指标 2（10 分）/ 决赛性能指标 2（10 分）",
        "judge": "rag",
    },
    "multihop": {
        "file": "rag_multihop.jsonl",
        "title": "RAG 多文档问答",
        "scoring": "决赛性能指标 2（多文档 / 多跳）",
        "judge": "rag",
    },
    "robust": {
        "file": "robust_cases.jsonl",
        "title": "系统鲁棒性",
        "scoring": "初赛优化指标 2（10 分）/ 决赛优化指标 4（5 分）",
        "judge": "sql",
    },
}

#: 赛题给的三档目标线。初赛与决赛的阈值相同，只是分值不同。
BANDS = (0.70, 0.80, 0.90)

#: 中级任务能力清单。它和上面的 SUITES 是**两件事**：
#:   · SUITES 是「现在就能跑一遍」的评测集，跑完出分数，随时间会变；
#:   · 本文件是「已经跑出来、有报告可查」的证据台账，是静态登记的事实。
#: 为什么分开：中级任务的证据分散在 T1/T3/T4/T5/T7 各自的专项报告里
#: （有的是 rubric 逐项检查、有的是严格结果匹配、有的是退化输入分类），
#: 口径不统一、也不能在几十秒内重跑一遍。面板要能说清「我们攻克了哪几个难题」，
#: 靠的是这份台账，不是再跑一次。
#: 台账只登记有报告可查的结论；查不到的写进 known_gaps，不编。
TASKS_EVIDENCE_PATH = ROOT / "infra" / "tasks_evidence.json"


def tasks_evidence() -> dict[str, Any]:
    """读中级任务证据台账。文件坏了返回带错误的空结构，前端据此提示。"""
    try:
        raw = json.loads(TASKS_EVIDENCE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"tasks": [], "error": "未找到 infra/tasks_evidence.json"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"tasks": [], "error": f"读取中级任务证据失败：{exc}"}
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        return {"tasks": [], "error": "infra/tasks_evidence.json 结构不对：缺少 tasks 数组"}
    return raw


def _eval_modules():
    """把 `eval/` 挂进 `sys.path` 并导入判定器。

    延迟导入有两个理由：一是 `app` 包在正常问答链路上不该依赖 `eval/`（评测是
    可选能力，缺了它问答照样跑）；二是 runner 顶层会解析方言、建执行器，
    启动即导入会让 uvicorn 冷启动平白变慢。
    """
    eval_dir = str(ROOT / "eval")
    if eval_dir not in sys.path:
        sys.path.insert(0, eval_dir)
    import metrics  # type: ignore[import-not-found]
    import rag_metrics  # type: ignore[import-not-found]
    import runner  # type: ignore[import-not-found]

    return runner, metrics, rag_metrics


def load_suite_cases(name: str) -> list[dict[str, Any]]:
    """读一个套件的用例。文件不存在时返回空列表（由调用方决定怎么提示）。"""
    if name not in SUITES:
        raise KeyError(f"未知套件：{name}")
    path = CASES_DIR / SUITES[name]["file"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def suite_catalog() -> list[dict[str, Any]]:
    """给面板用的套件清单：有哪些套件、各多少题、对应评分表哪一项。"""
    catalog = []
    for key, meta in SUITES.items():
        try:
            count = len(load_suite_cases(key))
        except Exception:  # noqa: BLE001 - 单个文件坏掉不该让整张清单挂掉
            count = 0
        catalog.append(
            {
                "key": key,
                "title": meta["title"],
                "scoring_item": meta["scoring"],
                "case_file": f"eval/cases/{meta['file']}",
                "case_count": count,
                "judge": meta["judge"],
            }
        )
    return catalog


@dataclass
class RunState:
    """一次评测运行的进度。跨线程读写，用锁保护。"""

    run_id: str
    suite: str
    total: int
    started_at: float = field(default_factory=time.perf_counter)
    done: int = 0
    finished: bool = False
    failed: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: 停止标志。置位后后台线程在**每道题之间**检查一次并收尾退出。
    #:
    #: 为什么是"题与题之间"而不是立刻中断：一题内部要过大模型、跑 SQL、
    #: 再判分，中途强杀会把连接和事务留在半开状态；跑完当前这题再退，
    #: 最多多等一题的时长，换来的是不会把数据库连接和 SSE 弄乱。
    cancel: threading.Event = field(default_factory=threading.Event)
    #: 是被用户停止的，还是自然跑完的。界面据此区分"已停止"与"已完成"。
    cancelled: bool = False

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0


#: 结果集预览最多带几行。514 页那档的用例结果可能几千行，整个塞进 SSE
#: 会把流撑爆，而且界面也放不下 —— 判分不依赖它，它纯粹是给人看的。
PREVIEW_ROWS = 5


def _cell_text(value: Any) -> str:
    """把单元格转成短文本。长字符串截断，None 显式写成 NULL。

    `None` 不能直接变空串：空串和空值在界面上长得一样，但它们是不同的结果。
    """
    if value is None:
        return "NULL"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _preview_rows(rows: list[Any] | None) -> list[list[str]]:
    """取结果集前若干行做预览。行本身可能是 tuple 或 list。"""
    if not rows:
        return []
    out: list[list[str]] = []
    for row in rows[:PREVIEW_ROWS]:
        if isinstance(row, (list, tuple)):
            out.append([_cell_text(cell) for cell in row])
        else:
            out.append([_cell_text(row)])
    return out


def _first_difference(gold_rows: list[Any] | None, pred_rows: list[Any] | None) -> str:
    """找第一处差异，说清「第几行第几列，标准是什么、实际是什么」。

    `Verdict.reason` 只描述**差异的性质**（「结果集数值不一致」），不指出差在哪；
    判成 fail 时那句话对评委没有可核对的内容。这里补上可核对的那一句。

    刻意逐行比而不是排序后比：排序后的语义是「集合不同」，但人看的是自己
    那条 SQL 的输出，逐行位置才对得上。
    """
    gold = _preview_rows(gold_rows)
    pred = _preview_rows(pred_rows)
    if not gold and pred:
        return "标准结果为空集，预测返回了数据。"
    if gold and not pred:
        return "标准结果有数据，预测返回空集。"
    if not gold and not pred:
        return "两侧都是空集。"
    for index, (gold_row, pred_row) in enumerate(zip(gold, pred), start=1):
        if gold_row == pred_row:
            continue
        width = max(len(gold_row), len(pred_row))
        for column in range(width):
            left = gold_row[column] if column < len(gold_row) else "（无此列）"
            right = pred_row[column] if column < len(pred_row) else "（无此列）"
            if left != right:
                return f"第 {index} 行第 {column + 1} 列：标准 = {left}，实际 = {right}"
        return f"第 {index} 行：标准 = {' | '.join(gold_row)}，实际 = {' | '.join(pred_row)}"
    if len(gold_rows or []) != len(pred_rows or []):
        return (
            f"前 {min(len(gold), len(pred))} 行一致，但行数不同"
            f"（标准 {len(gold_rows or [])} 行，实际 {len(pred_rows or [])} 行）。"
        )
    # 走到这里说明预览内的**值**完全一样，但判分仍然判了 fail。
    # 那差异就不在行数据上，而在列名或列序 —— 面板另有一行会显示「列序不同」，
    # 这里说清楚方向，别让人以为要去后面几行找。
    return "预览范围内每行取值都一致，差异在列名或列序上（不是数据本身）。"


class EvalRunner:
    """跑一次评测，边跑边把进度交给调用方（SSE 生成器消费 `progress_events`）。

    单进程内只允许一个评测在跑：评测会打满真实链路（每题都要过大模型），
    与其让两个评测互相把时延拉长、让界面上的耗时失去意义，不如直接串行。
    """

    def __init__(self) -> None:
        self._state: RunState | None = None
        self._lock = threading.Lock()

    def current(self) -> RunState | None:
        return self._state

    def is_busy(self) -> bool:
        state = self._state
        return state is not None and not state.finished

    def start(self, suite: str) -> RunState:
        self._purge_finished()
        with self._lock:
            if self.is_busy():
                raise RuntimeError("已有评测在运行中，请先停止它再开新的")
            cases = load_suite_cases(suite)
            if not cases:
                raise RuntimeError(f"套件 {suite} 没有可用用例（文件缺失或为空）")
            run_id = f"{suite}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            state = RunState(run_id=run_id, suite=suite, total=len(cases))
            self._state = state
        threading.Thread(target=self._execute, args=(state, cases), daemon=True).start()
        return state

    def cancel(self, run_id: str | None = None) -> bool:
        """请后台线程停下来。返回"有没有真的发出停止请求"。

        为什么要指定 run_id：界面点「停止」到后端收到请求之间，上一轮可能刚好
        自己跑完了、下一轮已经开始。若不核对就置位，会把**刚开的那一轮**一起停掉。
        所以只有 id 对上（或调用方明确不关心）才停。

        返回 True 只表示"已请求停止"，**不表示"已经停了"** —— 线程要跑完当前
        这题才会退出。真实是否结束以 `is_busy()` 为准，界面轮询它。
        """
        state = self._state
        if state is None or state.finished:
            return False
        if run_id is not None and state.run_id != run_id:
            return False
        state.cancel.set()
        return True

    def _purge_finished(self) -> None:
        """把已经跑完的那一轮从槽位上摘掉。

        为什么需要：`_state` 一直指着上一次的 RunState，界面下次进来问
        `/api/eval/suites` 会看到 `running: false`，但槽位本身没释放只是巧合
        无害；真正的问题是上一轮的 results 会被一直持有，连跑几次就是几份
        完整评测结果堆在内存里。摘掉即释放。
        """
        state = self._state
        if state is not None and state.finished:
            self._state = None

    # ── 后台线程 ────────────────────────────────────────────────────────
    def _execute(self, state: RunState, cases: list[dict[str, Any]]) -> None:
        try:
            runner, metrics, rag_metrics = _eval_modules()
            executor = _executor_singleton(runner)
            judge = SUITES[state.suite]["judge"]
            for case in cases:
                # 每道题开跑前检查一次停止标志。放这里而不是题内，是因为题内
                # 中途强杀会把大模型连接和 SQL 事务留在半开状态。
                if state.cancel.is_set():
                    with state.lock:
                        state.cancelled = True
                    break
                record = self._run_one(runner, metrics, rag_metrics, executor, judge, case)
                with state.lock:
                    state.results.append(record)
                    state.done = len(state.results)
            with state.lock:
                # 被停掉时不生成 summary：题目没跑完，准确率算出来是残缺的，
                # 摆一个"得分 3/15"出来会被误当成真实成绩。界面只显示已跑部分。
                if not state.cancelled:
                    state.summary = self._summarize(state)
        except Exception as exc:  # noqa: BLE001 - 后台异常必须落到 state，否则界面永远转圈
            with state.lock:
                state.failed = f"{type(exc).__name__}: {exc}"
        finally:
            with state.lock:
                state.finished = True

    def _run_one(self, runner, metrics, rag_metrics, executor, judge: str, case: dict[str, Any]) -> dict[str, Any]:
        """跑一题：真实链路取答案 → 用 runner 的判定口径判分。"""
        question = case["question"]
        record: dict[str, Any] = {
            "id": case["id"],
            "question": question,
            "difficulty": case.get("difficulty"),
        }
        started = time.perf_counter()
        try:
            response = _orchestrator_singleton().answer(question, f"panel-{case['id']}")
        except Exception as exc:  # noqa: BLE001
            record.update(elapsed_ms=(time.perf_counter() - started) * 1000, passed=False)
            record["reason"] = f"链路异常：{type(exc).__name__}: {exc}"
            return record

        record["elapsed_ms"] = (time.perf_counter() - started) * 1000
        record["answer"] = response.answer
        record["tools"] = [step.tool for step in response.trace]
        record["need_clarify"] = response.need_clarify
        record["elapsed_s"] = round(record["elapsed_ms"] / 1000, 2)

        if judge == "sql":
            record.update(self._judge_sql(metrics, executor, case, response))
        else:
            record.update(self._judge_rag(runner, rag_metrics, case, response))
        return record

    def _judge_sql(self, metrics, executor, case: dict, response) -> dict[str, Any]:
        """SQL 类套件：执行模型产出的 SQL，与 gold 结果集比对。

        刻意**重新执行**模型给的 SQL，而不是信任 `response.answer` 里的文字：
        `answer` 是给人看的自然语言，`sql` 才是可判定的产物。

        行序是否参与判定，与 runner 同口径：看 gold 是否含 `ORDER BY`
        （`gold_sql_has_order_by`）。列语义（`column_types`，如 money 的 1 分钱容差）
        也照样传下去，否则面板会比报告更严或更松。
        """
        sql = response.sql or ""
        out: dict[str, Any] = {"sql": sql}
        if not sql.strip():
            out.update(passed=False, reason="未产出 SQL")
            return out
        pred_rows, pred_err = executor.execute(sql)
        gold_rows, gold_err = executor.execute(case["gold_sql"])
        verdict = metrics.compare_resultsets(
            gold_rows,
            pred_rows,
            gold_error=gold_err,
            pred_error=pred_err,
            order_matters=metrics.gold_sql_has_order_by(case.get("gold_sql") or ""),
            column_types=case.get("column_types"),
        )
        # 注意属性名是 `ok` 不是 `passed` —— 见 eval/metrics.py:186，它把 `pass` 与
        # `pass_loose`（仅列序不同）都算通过，用错名字会直接抛 AttributeError。
        out["passed"] = bool(verdict.ok)
        out["verdict_kind"] = verdict.kind
        out["reason"] = verdict.reason or ("结果一致" if out["passed"] else "结果不一致")
        # `pass_loose` = 只有列序不同。判过但报告里单独计数、可人工复核，面板上也要看得见。
        out["loose"] = verdict.kind == "pass_loose"
        if verdict.column_permutation:
            out["column_permutation"] = list(verdict.column_permutation)

        # ── 给人看的过程留痕 ──────────────────────────────────────────
        # `Verdict` 只带行数不带值（见 eval/metrics.py:175），判成 fail 时界面
        # 只能显示「结果集数值不一致」这种性质描述，评委没法自己对一眼。
        # 所以在这里补三样：gold 的 SQL 与结果、预测的结果、首处差异。
        # 只截前若干行：结果集可能几千行，整个塞进 SSE 会把流撑爆。
        out["gold_sql"] = case.get("gold_sql") or ""
        out["gold_rows_count"] = verdict.gold_rows
        out["pred_rows_count"] = verdict.pred_rows
        out["gold_preview"] = _preview_rows(gold_rows)
        out["pred_preview"] = _preview_rows(pred_rows)
        if gold_err:
            out["gold_error"] = str(gold_err)
        if pred_err:
            out["pred_error"] = str(pred_err)
        if not out["passed"]:
            out["first_diff"] = _first_difference(gold_rows, pred_rows)
        return out

    def _judge_rag(self, runner, rag_metrics, case: dict, response) -> dict[str, Any]:
        """RAG 类套件：按口径 3.3 判 —— 要点全中 + 禁答实体不出现 + 有合格引用。"""
        known_doc_ids = runner.load_known_doc_ids()
        answer_refs = [
            ref.model_dump(mode="json") for ref in response.refs if getattr(ref, "kind", None) == "doc"
        ]
        verdict = rag_metrics.judge_case(
            case, response.answer or "", answer_refs, known_doc_ids=known_doc_ids
        )
        details = verdict.details or {}
        out: dict[str, Any] = {
            "passed": bool(verdict.passed),
            "reason": _rag_reason(verdict),
            # 三个子条件分开留痕：演示时要能回答「这题为什么不算对」。
            "sub": {
                "points_hit": details.get("points_hit"),
                "points_total": details.get("points_total"),
                "missed_points": details.get("missed_points") or [],
                "hallucinated_entities": details.get("hallucinated_entities") or [],
                "citation_ok": verdict.citation_ok,
                "citation_basis": verdict.citation_basis,
            },
        }
        # 判分要点与期望来源：判成 fail 时界面要能说「该答到的没答到」，
        # 光有 `missed_points` 不够 —— 那是缺的那几条，还要让评委看到
        # **期望引用哪份文档的哪一节**，否则「引用不合格」也无从核对。
        out["answer_points"] = [
            item.get("point") for item in (case.get("answer_points") or []) if item.get("point")
        ]
        out["forbidden_entities"] = list(case.get("forbidden_entities") or [])
        out["gold_refs"] = [
            {
                "doc": ref.get("doc") or "",
                "anchor": ref.get("anchor") or "",
            }
            for ref in (case.get("gold_refs") or [])
            if isinstance(ref, dict) and (ref.get("doc") or ref.get("anchor"))
        ]
        return out

    # ── 汇总 ────────────────────────────────────────────────────────────
    def _summarize(self, state: RunState) -> dict[str, Any]:
        """汇总成面板要的形状：总数、通过数、准确率、评分表三档目标。"""
        results = state.results
        total = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        rate = (passed / total) if total else 0.0

        by_difficulty: dict[str, dict[str, Any]] = {}
        for r in results:
            key = r.get("difficulty") or "未标注"
            bucket = by_difficulty.setdefault(key, {"total": 0, "passed": 0})
            bucket["total"] += 1
            bucket["passed"] += 1 if r.get("passed") else 0
        for bucket in by_difficulty.values():
            bucket["accuracy"] = round(bucket["passed"] / bucket["total"], 4) if bucket["total"] else 0.0

        latencies = sorted(r.get("elapsed_ms", 0.0) for r in results)
        meta = SUITES[state.suite]
        return {
            "run_id": state.run_id,
            "suite": state.suite,
            "suite_title": meta["title"],
            "scoring_item": meta["scoring"],
            "total": total,
            "passed": passed,
            "accuracy": round(rate, 4),
            # 赛题三档：≥70% / ≥80% / ≥90%（初赛与决赛档位线一致）
            "bands": [
                {"threshold": band, "label": f"≥{int(band * 100)}%", "reached": rate >= band}
                for band in BANDS
            ],
            "by_difficulty": by_difficulty,
            "latency": {
                "p50_ms": latencies[len(latencies) // 2] if latencies else 0.0,
                "max_ms": latencies[-1] if latencies else 0.0,
            },
            "total_elapsed_ms": state.elapsed_ms(),
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "scope": "面板演示口径：单次运行、真实链路、判分复用 eval/runner 的口径",
        }


def _rag_reason(verdict) -> str:
    """把三个子条件翻译成人话，说明这题为什么过 / 不过。"""
    if verdict.passed:
        return "要点全中、无禁答实体、引用合格"
    details = verdict.details or {}
    reasons = []
    missed = details.get("missed_points") or []
    if missed:
        reasons.append("未答到：" + "；".join(str(item) for item in missed[:2]))
    hallucinated = details.get("hallucinated_entities") or []
    if hallucinated:
        reasons.append("出现禁答实体：" + "、".join(str(item) for item in hallucinated[:3]))
    if not verdict.citation_ok:
        reasons.append("引用不合格（缺来源或指向未知文档）")
    return "；".join(reasons) if reasons else "未通过"


# ── 进程级单例：评测与手动提问必须共用同一份资源 ────────────────────────
_orchestrator_lock = threading.Lock()
_orchestrator_cache: Any = None
_executor_lock = threading.Lock()
_executor_cache: dict[str, Any] = {}


def _orchestrator_singleton():
    """复用 `app.main` 里那一个编排器实例。

    必须复用：评测与手动提问打的是同一个库、同一份索引。各建一个实例会让
    向量索引被加载两遍，内存翻倍且首题平白多等一次冷启动。
    同时它会带上当前进程的会话记忆，所以每题的 session_id 用 `panel-<用例id>`
    隔开，避免上一题的上下文污染下一题 —— 那会让「单题准确率」失去意义。
    """
    global _orchestrator_cache
    with _orchestrator_lock:
        if _orchestrator_cache is None:
            from app.main import _orchestrator  # 复用主入口那个进程级单例

            _orchestrator_cache = _orchestrator
        return _orchestrator_cache


def _executor_singleton(runner):
    """按方言缓存 SQL 执行器。每次现建会重复解析 DSN 并重连。

    方言必须从**解析后的 DSN** 推断，不能只看 `DATABASE_URL`：本项目的真正常用
    连接串记在 `.env` 的 `PG_DSN` 里，只看 `DATABASE_URL` 会静默回落到内存 SQLite
    ——`resolve_dialect` 的文档注释专门警告过这一点（它会从"真实 PostgreSQL"
    悄悄变成"sqlglot 转换后的内存 SQLite"，而退出码仍是 0，最难查）。
    """
    dsn = _panel_dsn()
    dialect, source = runner.resolve_dialect(None, dsn)
    if source == "auto_fallback":
        print(
            "[eval_api] 警告：未找到数据库连接串，判分将回落到内存 SQLite，"
            "结果与正式报告不可比。请确认 .env 里的 PG_DSN / DATABASE_URL。",
            file=sys.stderr,
        )
    with _executor_lock:
        if dialect not in _executor_cache:
            _executor_cache[dialect] = runner.make_executor(dialect, dsn)
        return _executor_cache[dialect]


def _panel_dsn() -> str | None:
    """面板判分用的连接串。

    与 `eval/runner.py` 同一个来源：`DATABASE_URL` 优先，其次按 `.env` 里那套
    PostgreSQL 约定拼（与问答链路连的是同一个 `chinook` 库）。
    **必须和 `app/nl2sql/config.py` 的默认值同源**，否则会出现
    「模型查 A 库、判分查 B 库」这种最难查的不一致。
    """
    from app.nl2sql.config import load_env_file

    load_env_file()
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    dsn = os.getenv("PG_DSN")
    if dsn and dsn.startswith("postgresql://"):
        return dsn
    user = os.getenv("POSTGRES_USER", "ict")
    password = os.getenv("POSTGRES_PASSWORD", "ict_dev_only_change_me")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "chinook")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def progress_events(state: RunState) -> Iterator[str]:
    """把一次运行变成 SSE 事件流。

    帧格式与前端既有解析器一致：`data: {json}\\n\\n`，靠 `type` 分支。
    这里不复用 `/api/ask` 的那套事件名（step / text / evidence），因为
    评测面板是独立的流，混在一起会让前端解析器难写。
    """
    sent = 0
    last_heartbeat = time.perf_counter()
    while True:
        with state.lock:
            pending = state.results[sent:]
            finished = state.finished
            failed = state.failed
            summary = state.summary
            cancelled = state.cancelled
        for record in pending:
            sent += 1
            yield _frame({"type": "case", "index": sent, "total": state.total, "data": record})
        if failed:
            yield _frame({"type": "error", "data": failed})
            return
        if finished:
            # 被停止时不发 done：done 的语义是"跑完了，这是最终成绩"，而停止
            # 之后没有最终成绩。发一个 cancelled 让界面把状态写成"已停止"，
            # 而不是把半截结果当成绩展示。
            if cancelled:
                yield _frame({"type": "cancelled", "index": sent, "total": state.total})
            else:
                yield _frame({"type": "done", "data": summary or {}})
            return
        # 心跳：文档链路单题可能十几秒没有事件，代理与浏览器会掐掉静默连接。
        now = time.perf_counter()
        if now - last_heartbeat > 5.0:
            last_heartbeat = now
            with state.lock:
                done = state.done
            yield _frame({"type": "progress", "index": done, "total": state.total})
        time.sleep(0.4)


def _frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
