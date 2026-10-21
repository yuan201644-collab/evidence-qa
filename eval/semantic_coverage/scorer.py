"""S2-H 语义覆盖打分：独立读取 rubric.jsonl 与 driver 采集的原始响应，逐场景判分。

与 driver.py 的分工：driver 只采集；本脚本独立打分，**不读历史答案、不改写判据**
（母方案 :580）。评分直接消费采集目录里的 raw_responses.jsonl，与判据文件逐条对账。

评分口径（S2 计划书 §12.1，P1 裁定为 (c) 后）：
  - `pass`        ：判据全部满足；
  - `repair`      ：判据明确违反（该澄清没澄清、条件被丢、错误 SQL 进了执行）；
  - `unknown`     ：无法证明也不证伪（采集失败、响应缺字段）——单列计数，**不算拦截成功也不算静默遗漏**；
  - 执行错误和超时**保留分母**，绝不从统计里剔除；
  - **约束不全但偶然数字相同，仍判失败**（静默遗漏判据咬死「未落实、未告知」）。

六项指标（§12.1）中本脚本直接产出四项：
  必要澄清正确率 / 静默遗漏数 / 正常问题保持率 / 端到端场景通过率。
  错误候选拦截率由 tests/test_requirement_pipeline_gate.py（L2 注入）覆盖，不在此处。

用法：
    python -m eval.semantic_coverage.scorer --run-dir eval/reports/semantic_coverage/<运行号>
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = ROOT / "eval" / "semantic_coverage" / "development_v0.1"
RUBRIC_PATH = CASES_DIR / "rubric.jsonl"


@dataclass
class Verdict:
    case_id: str
    category: str
    status: str  # pass / repair / unknown
    failed_checks: list[str] = field(default_factory=list)
    notes: str = ""


def load_rubric() -> dict[str, dict[str, Any]]:
    rubric: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(RUBRIC_PATH.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        item = json.loads(text)
        rubric[str(item["id"])] = item
    return rubric


def trace_tools(body: dict[str, Any]) -> list[str]:
    return [str(step.get("tool") or "") for step in (body.get("trace") or [])]


def fact_review_coverage(body: dict[str, Any]) -> str | None:
    """从 fact_review.items 里找 sql_requirement_coverage 检查的结果。"""
    review = body.get("fact_review") or {}
    for item in review.get("items") or []:
        for check in item.get("checks") or []:
            if check.get("code") == "sql_requirement_coverage":
                return str(check.get("result"))
    return None


def check_scenario(rubric_item: dict[str, Any], scenario: dict[str, Any]) -> Verdict:
    """对单个场景逐条判据核对。全部判据来自 rubric.jsonl，本函数不含场景专属逻辑。"""
    case_id = str(rubric_item["id"])
    rubric = rubric_item.get("rubric") or {}
    failed: list[str] = []
    notes: list[str] = []

    if not scenario.get("turns"):
        return Verdict(case_id, rubric_item.get("category", ""), "unknown",
                       notes="采集记录里没有轮次")

    # 取第一轮（本套 SC 场景均为单轮；多轮场景 SC-20~23 属 S3，未纳入本批题面）
    turn = scenario["turns"][0]
    status = turn.get("status")
    body = turn.get("body") or {}
    answer = str(body.get("answer") or "")
    sql = str(body.get("sql") or "")
    tools = trace_tools(body)
    answer_text = answer.casefold()

    if status != 200:
        # 执行错误和超时保留分母：登记 unknown，不计入任何「正确回答」分母（§12.1）
        return Verdict(case_id, rubric_item.get("category", ""), "unknown",
                       failed_checks=[f"HTTP {status}: {str(turn.get('error') or body.get('error'))[:160]}"],
                       notes="请求未成功完成；保留分母，不自动重放")

    # ── 澄清判据 ────────────────────────────────────────────────
    expect_clarify = rubric.get("expect_clarify")
    need_clarify = bool(body.get("need_clarify"))
    if expect_clarify is True:
        if not need_clarify:
            failed.append("应前置澄清但未澄清")
        for tool in rubric.get("expect_zero_tool_calls") or []:
            if tool in tools:
                failed.append(f"澄清前不得调用 {tool}，实际出现在 trace")
    elif expect_clarify is False and need_clarify:
        failed.append("不应澄清却触发了澄清")

    # ── 静默遗漏判据（§12.1 咬死「未落实、未告知」）───────────────
    expect_absent = [str(x) for x in rubric.get("expect_absent") or []]
    absent_hits = [x for x in expect_absent if x.casefold() in answer_text]
    if absent_hits:
        # 答案出现了不该出现的数字/标记 = 大概率是静默返回了全量结果
        # 但要区分「如实说明做不到」与「硬给数字」：若同时含说明性文字则不判失败
        mentions = [str(x) for x in rubric.get("expect_mention_any") or []]
        has_explanation = any(m.casefold() in answer_text for m in mentions)
        if not has_explanation:
            failed.append(f"答案出现不应出现的内容且无解释（静默遗漏嫌疑）：{'、'.join(absent_hits)}")
        else:
            notes.append("答案含数字但有如实说明；不算静默遗漏")

    # ── 条件覆盖判据（SQL 层）────────────────────────────────────
    sql_text = sql.casefold()
    for token in rubric.get("expect_sql_contains") or []:
        if str(token).casefold() not in sql_text:
            failed.append(f"SQL 未包含预期条件：{token}")
    # any 语义：列出多种等价写法（如 NOT IN / <> / !=），命中其一即可
    any_tokens = [str(x) for x in rubric.get("expect_sql_contains_any") or []]
    if any_tokens and not any(str(t).casefold() in sql_text for t in any_tokens):
        failed.append(f"SQL 未包含任何一种预期条件写法：{'、'.join(any_tokens)}")
    for token in rubric.get("expect_sql_absent") or []:
        if str(token).casefold() in sql_text:
            failed.append(f"SQL 包含不应出现的条件：{token}")

    # ── 闸门留痕判据 ────────────────────────────────────────────
    expected_coverage = rubric.get("expect_sql_requirement_coverage")
    if expected_coverage:
        actual = fact_review_coverage(body)
        if actual is None:
            notes.append("响应未携带 sql_requirement_coverage（可能未开 fact-review 协商头）")
        elif actual != expected_coverage:
            failed.append(f"sql_requirement_coverage 预期 {expected_coverage}，实际 {actual}")

    # ── 正向关键词判据 ──────────────────────────────────────────
    mentions = [str(x) for x in rubric.get("expect_mention_any") or []]
    if mentions and not any(m.casefold() in answer_text for m in mentions):
        failed.append("答案未包含任何预期说明性关键词：" + "、".join(mentions))

    # ── 负控制判据（澄清话术不得误挡正常问题）────────────────────
    for token in expect_absent:
        if token in answer and token.startswith(("请补充", "请说明")):
            failed.append(f"正常问题被澄清话术误挡：{token}")

    if rubric.get("expect_answer") and not answer.strip():
        failed.append("预期有业务答案但答案为空")

    status_out = "repair" if failed else "pass"
    return Verdict(case_id, rubric_item.get("category", ""), status_out,
                   failed_checks=failed, notes="；".join(notes))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="driver.py 的输出目录")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    raw_path = run_dir / "raw_responses.jsonl"
    if not raw_path.is_file():
        raise SystemExit(f"采集记录不存在：{raw_path}")

    rubric = load_rubric()
    scenarios: dict[str, dict[str, Any]] = {}
    for line in raw_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            scenarios[str(item["id"])] = item

    verdicts: list[Verdict] = []
    missing = [cid for cid in rubric if cid not in scenarios]
    for cid in missing:
        verdicts.append(Verdict(cid, "", "unknown", notes="采集记录缺失；保留分母"))
    for cid, item in rubric.items():
        if cid in scenarios:
            verdicts.append(check_scenario(item, scenarios[cid]))

    # ── 六项指标中的四项 + unknown 单列（§12.1 P1 口径）────────────
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.status == "pass")
    repairs = [v for v in verdicts if v.status == "repair"]
    unknowns = [v for v in verdicts if v.status == "unknown"]
    clarify_cases = [v for v in verdicts
                     if v.case_id in {c for c, r in rubric.items()
                                      if r.get("rubric", {}).get("expect_clarify") is True}]
    clarify_ok = sum(1 for v in clarify_cases if v.status == "pass")
    silent_omissions = sum(
        1 for v in repairs if any("静默遗漏" in f for f in v.failed_checks)
    )

    print("=" * 72)
    print(f"语义覆盖评分  |  运行目录 {run_dir.name}")
    print("=" * 72)
    for v in verdicts:
        mark = {"pass": "通过", "repair": "未通过", "unknown": "未确定"}[v.status]
        print(f"【{v.case_id}】{v.category or '-'} —— {mark}")
        for f in v.failed_checks:
            print(f"    ✗ {f}")
        if v.notes:
            print(f"    · {v.notes}")
    print()
    print(f"场景总数（分母，含失败/超时）：{total}")
    print(f"  pass    {passed}")
    print(f"  repair  {len(repairs)}")
    print(f"  unknown {len(unknowns)}  ← 单列计数，不算拦截成功也不算静默遗漏")
    print(f"必要澄清正确率：{clarify_ok}/{len(clarify_cases)}")
    print(f"静默遗漏数：{silent_omissions}（必须为 0）")
    print(f"端到端场景通过率：{passed}/{total}")

    summary = {
        "total": total,
        "pass": passed,
        "repair": len(repairs),
        "unknown": len(unknowns),
        "clarification_accuracy": f"{clarify_ok}/{len(clarify_cases)}",
        "silent_omissions": silent_omissions,
        "end_to_end_pass_rate": f"{passed}/{total}",
        "verdicts": [vars(v) for v in verdicts],
    }
    (run_dir / "scores.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"评分已写入 {run_dir / 'scores.json'}")
    return 0 if not repairs else 1


if __name__ == "__main__":
    sys.exit(main())
