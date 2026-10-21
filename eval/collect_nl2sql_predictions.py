"""进程内采集 NL2SQL 真实预测，产出可直接喂 `eval/runner.py --predictor jsonl` 的文件。

为什么需要这个脚本（2026-10-17 定）
------------------------------------
`eval/collect_rag_predictions.py` 已经用「路二」（进程内调编排层）打通了 RAG 两条套件，
但结构化问数（`nl2sql` / `robust`）从未在真实链路上采集过 —— 此前所有
100% 的数字都来自 `--predictor oracle`（回放 gold SQL 自我验证），
与「系统真实表现」不是一回事。本脚本补上这块空白。

三条路为什么不选（与 RAG 脚本同一套理由）
------------------------------------------
| 路 | 做法 | 否决理由 |
|---|---|---|
| 甲 | 起服务后 `--predictor http` | `HttpPredictor`（eval/runner.py:393）与真实 `/api/ask` 三处不匹配：无鉴权、请求体形状不同、响应假设 `{sql,...}` 而真实返回契约 `AskResponse` |
| 乙 | 直接调 `Nl2SqlPipeline` | 绕过了编排层的澄清与路由，测出来的不是「用户真实得到的东西」 |
| 丙 | **编排层进程内调用（本脚本）** | 采到的是含澄清、路由、定位、生成、校验、执行的完整链路产物 |

口径诚实性
----------
`sql` 字段取自 trace 里**真正被执行**的那条语句，不是模型原始输出。
若链路以澄清结束（`clarified=True`）或报错，则该题记 `error` 并保留话术，
不伪造一条 SQL 去凑分 —— 错误、超时、空答、不必要拒答都留在分母里
（口径见 `eval/mainline/README.md`「判分边界」）。

用法
----
    # 结构化问数（15 题）
    D:\\python\\python.exe -X utf8 eval\\collect_nl2sql_predictions.py --suite nl2sql

    # 鲁棒性（18 题）
    D:\\python\\python.exe -X utf8 eval\\collect_nl2sql_predictions.py --suite robust

    # 两个都跑
    D:\\python\\python.exe -X utf8 eval\\collect_nl2sql_predictions.py --suite all

产出落在 `eval/reports/nl2sql_real_predictions_<suite>_<UTC时间戳>.jsonl`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES_DIR = ROOT / "eval" / "cases"
REPORTS_DIR = ROOT / "eval" / "reports"

#: 结构化两条套件都用默认的 `real` 问数后端，只换题集。
SUITE_SPEC = {
    "nl2sql": {"cases": "nl2sql.jsonl", "label": "结构化问数"},
    "robust": {"cases": "robust_cases.jsonl", "label": "鲁棒性"},
    # 政采（真实政务数据）：题集在子目录里，题目更多、表更多，见该目录 README。
    "gov": {"cases": "gov_procurement/nl2sql.jsonl", "label": "政采结构化问数"},
    # 政采鲁棒性（错别字/别名/时序/澄清）挂在同一套政采库上。
    "gov_robust": {"cases": "gov_procurement/robust_cases.jsonl", "label": "政采鲁棒性"},
    # 政采跨源融合：一题同时要查库和查文档，判分走 eval/gov_cross_runner.py，
    # 不跑实体的结果集比对。这里只负责把系统答案采回来。
    "gov_cross": {"cases": "gov_procurement/cross_source.jsonl", "label": "政采跨源融合"},
}


def _load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def _trace_steps(trace) -> list[dict]:
    """把契约 trace 归一成 dict 列表，兼容对象与已序列化的 dict 两种形状。"""
    steps: list[dict] = []
    for step in trace or []:
        if hasattr(step, "model_dump"):
            steps.append(step.model_dump(mode="json"))
        elif isinstance(step, dict):
            steps.append(dict(step))
    return steps


def _extract_sql(steps: list[dict]) -> tuple[str | None, str | None]:
    """从 trace 里取「真正被执行」的 SQL，返回 `(sql, 来源说明)`。

    优先级刻意按「是否真的执行过」排：

    1. `execute_sql` 类步骤的**入参** —— 这是送给数据库的那一条，最可信；
    2. `nl2sql` / `sql_generate` 步骤的输出 `sql` —— 生成结果，但可能未及执行
       （校验拦下、重试耗尽）。只在没有执行步骤时兜底取**最后一次**，
       因为重试链里最后一次才是链路最终判定为合格的那条；
    3. 都没有 -> `(None, None)`。

    之所以不直接取第一条生成结果：多轮重试下第一条往往是写错的，
    拿它去判分等于把系统自己判为不合格的答案当成交付物。
    """
    executed: list[str] = []
    generated: list[tuple[int, str]] = []

    for index, step in enumerate(steps):
        name = str(step.get("tool") or step.get("name") or "").lower()
        output = step.get("output") or {}
        inputs = step.get("input") or {}
        if not isinstance(output, dict):
            output = {}
        if not isinstance(inputs, dict):
            inputs = {}

        # ① 送进数据库的语句：工具名含 execute/query，入参里的 sql 就是它。
        if any(key in name for key in ("execute", "query_sql", "run_sql")):
            for key in ("sql", "statement", "query"):
                value = inputs.get(key) or output.get(key)
                if isinstance(value, str) and value.strip():
                    executed.append(value.strip())
                    break

        # ② 生成结果：本轮兜底用。
        if any(key in name for key in ("nl2sql", "sql_generate", "generate_sql")):
            value = output.get("sql")
            if isinstance(value, str) and value.strip():
                generated.append((index, value.strip()))

    if executed:
        return executed[-1], "execute_step"
    if generated:
        return generated[-1][1], "generation_output"
    return None, None


def _apply_time_anchor(case: dict) -> str:
    """按题目自带的 `time_anchor` 设置 `APP_TIME_ANCHOR`，返回当前生效值。

    为什么必须逐题设置（2026-10-17 实测）
    ------------------------------------
    `app/nl2sql/config.time_anchor_from_env()` 读环境变量 `APP_TIME_ANCHOR`；
    不设置时回落 `datetime.now()`。相对时间题（今年/上个月/本季度）的 gold 都以
    `2024-06-15` 为基准（docs/要素识别与改写_接口_v0.1.md §6.1 已拍板），
    用真实当天算出来的区间落在数据覆盖之外，结果恒为 0 —— 那是**采错的 0**，
    不是系统答错。本轮首次跑鲁棒性时就踩了这个坑（rob-14~rob-18 全返回 0）。

    ⚠️ 同时必须记住一个数据层面的坑：Chinook 里 2021–2024 四年的发票数**都是 83**，
    所以「今年/去年/前年」这三类题在年份算错时仍可能得到相同答案（假阳性）。
    实测 rob-12/rob-13 就是这样被判通过的：预测 SQL 用的是 2024 区间，
    而题目要的是 2023/2022。本函数只负责把基准设对，
    **无法**弥补题集本身的鉴别力缺陷 —— 那一条须在报告里如实标注。
    """
    anchor = str(case.get("time_anchor") or "").strip()
    if anchor:
        os.environ["APP_TIME_ANCHOR"] = anchor
        return anchor
    # 题目没声明锚点：清掉上一题留下的值，避免跨题串味。
    os.environ.pop("APP_TIME_ANCHOR", None)
    return "(系统当前时间)"


def collect(suite: str, *, limit: int | None, verbose: bool) -> Path:
    spec = SUITE_SPEC[suite]
    cases_path = CASES_DIR / spec["cases"]
    if not cases_path.exists():
        raise SystemExit(f"题集不存在：{cases_path}")

    cases = _load_cases(cases_path)
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise SystemExit(f"{cases_path.name} 为空")

    from app.orchestrator import build_default_orchestrator
    from app.rag.config import loaded_dotenv_keys

    keys = loaded_dotenv_keys()
    print(f"[env] 从 .env 加载了 {len(keys)} 个键")
    print(f"[env] LLM_API_KEY={'已设置' if os.environ.get('LLM_API_KEY') else '**未设置**'}"
          f"  DATABASE_URL={'已设置' if os.environ.get('DATABASE_URL') else '**未设置**'}"
          f"  语义规划={'开' if os.environ.get('NL2SQL_SEMANTIC_PLANNING_ENABLED') not in (None, '', '0') else '关'}")
    print(f"[env] 要求覆盖闸门 NL2SQL_REQUIREMENT_GATE={os.environ.get('NL2SQL_REQUIREMENT_GATE', '(默认 warn)')}")

    # 只测 SQL ⊆ RAG 那条主链：图谱 mock 会给假结果，必须关掉。
    orchestrator = build_default_orchestrator(graph_backend="disabled")
    print(f"[编排] 结构化问数后端默认（NL2SQL_BACKEND={os.environ.get('NL2SQL_BACKEND', 'real')}）"
          f"  graph_backend=disabled")
    print(f"[题集] {cases_path.name}  共 {len(cases)} 题  （{spec['label']}）")
    print("-" * 78)

    rows: list[dict] = []
    ok = clarified = failed = 0
    # 跨源套件不产出「一条 SQL 就是答案」这种结果：一题要同时查库和查文档，
    # 所以对它的计数口径是「有没有答出内容」，不是「有没有 SQL」。
    cross_source = suite == "gov_cross"
    answered = 0
    t_all = time.perf_counter()

    for i, case in enumerate(cases, 1):
        cid = case["id"]
        question = case["question"]
        # 每题独立会话：鲁棒性题集里有 `canonical_question`，不能靠上一题的条件继承。
        session_id = f"collect-{suite}-{cid}"
        anchor_used = _apply_time_anchor(case)
        t0 = time.perf_counter()
        try:
            resp = orchestrator.answer(question, session_id=session_id, retrieval_mode="standard")
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            steps = _trace_steps(getattr(resp, "trace", None))
            sql, source = _extract_sql(steps)
            answer = getattr(resp, "answer", "") or ""
            # 契约里的字段名是 `need_clarify`（app/contracts.py），不是 `needs_clarification`。
            # 2026-10-17 修：原先只读后两个名字，导致澄清类题（rob-10/rob-11）
            # 明明澄清了却因缺 `clarified` 字段被判 fail —— 是脚本的错，不是系统的错。
            clarified_flag = bool(
                getattr(resp, "need_clarify", False)
                or getattr(resp, "needs_clarification", False)
                or getattr(resp, "clarified", False)
            )

            row: dict = {"id": cid, "latency_ms": elapsed_ms}
            if anchor_used != "(系统当前时间)":
                row["time_anchor"] = anchor_used
            if sql:
                row["sql"] = sql
                row["sql_source"] = source
                ok += 1
                status = f"OK  sql={len(sql):>4}字 {elapsed_ms:>6}ms [{source}]"
            elif cross_source:
                # 跨源题本来就不该通过「取到 SQL」来判成功：它的产物是答案文本。
                if answer:
                    answered += 1
                    status = f"OK  答案={len(answer):>4}字 {elapsed_ms:>6}ms"
                else:
                    failed += 1
                    status = f"**空答** {elapsed_ms:>6}ms"
            else:
                reason = getattr(resp, "clarify_question", None) or answer or "链路未产出 SQL"
                row["error"] = f"未产出 SQL：{str(reason)[:200]}"
                failed += 1
                status = f"**无SQL** {str(reason)[:70]}"
            # 跨源题以「有没有答出内容」为口径，独立于 SQL 是否取到。
            # 2026-10-17 修：原先把 answered 自增放在 `elif cross_source` 里，
            # 而跨源题实际上都能取到 SQL（nl2sql 那一半总是产出 SQL），
            # 于是永远走 SQL 分支、`elif` 从不触发，汇总恒显示「答出内容 0」。
            # 那是汇总口径的错，不是链路没答出内容。
            if cross_source and answer:
                if not sql:
                    pass  # 已在上面计入 answered
                else:
                    answered += 1
            # 答案与引用一并保留：报告里要能讲清楚「SQL 对了但回答没解释」这类情况。
            row["answer"] = answer
            row["refs"] = [
                r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r)
                for r in (getattr(resp, "refs", None) or [])
            ]
            # 无论是否产出 SQL 都必须写这个字段：runner 明确「缺字段不等于通过」。
            row["clarified"] = clarified_flag
            if clarified_flag:
                row["clarify_question"] = answer
        except Exception as exc:  # noqa: BLE001 - 采集要的是「哪题炸了」，不是崩整轮
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            row = {
                "id": cid,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": elapsed_ms,
            }
            failed += 1
            status = f"**FAIL** {type(exc).__name__}: {str(exc)[:80]}"
            if verbose:
                traceback.print_exc()

        rows.append(row)
        print(f"  [{i:>2}/{len(cases)}] {cid:<12} {status}")
        if verbose:
            print(f"           Q: {question[:76]}")
            if row.get("sql"):
                print(f"           SQL: {row['sql'][:76]}")

    total_s = time.perf_counter() - t_all

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = REPORTS_DIR / f"nl2sql_real_predictions_{suite}_{stamp}.jsonl"
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("-" * 78)
    if cross_source:
        print(f"[完成] 答出内容 {answered} / 空答 {failed} / 共 {len(rows)} 题，"
              f"总耗时 {total_s:.1f}s")
    else:
        print(f"[完成] 产出 SQL {ok} / 无 SQL {failed} / 共 {len(rows)} 题，总耗时 {total_s:.1f}s")
    print(f"[产出] {out}")
    print()
    print("下一步（判分）：")
    if cross_source:
        print(f"  D:\\python\\python.exe -X utf8 eval\\gov_cross_runner.py "
              f"--predictions \"{out}\" --tag cross")
    else:
        print(f"  D:\\python\\python.exe -X utf8 eval\\runner.py --suite {suite} "
              f"--predictor jsonl --predictions \"{out}\"")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="进程内采集 NL2SQL 真实预测（路线二）")
    ap.add_argument("--suite",
                    choices=["nl2sql", "robust", "gov", "gov_robust", "gov_cross",
                             "all", "gov_all"],
                    default="nl2sql")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题，用于冒烟")
    ap.add_argument("--verbose", action="store_true", help="打印问题与 SQL 摘要")
    args = ap.parse_args()

    suites = {
        "all": ["nl2sql", "robust"],
        # 政采单独成 all，避免与主库套件混在一次运行里 —— 两者数据集不同、
        # 分数不能相加，混跑容易在后处理时分不清哪题属于哪个库。
        "gov_all": ["gov", "gov_robust"],
    }.get(args.suite, [args.suite])
    for suite in suites:
        collect(suite, limit=args.limit, verbose=args.verbose)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
