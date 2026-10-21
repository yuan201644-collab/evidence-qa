#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统计三轮开发回归里「多表 SQL 是否真的过了 JOIN 外键校验」。

只读统计：解析每条候选 SQL 的 JOIN 数量，与 trace 中是否出现 `sql_semantic_validator`
比对，量化 sem-12 根因（语义校验默认关闭）的影响面。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

RUNS = {
    "T3 (b3c8041 带锚点正式重跑)":
        "eval/reports/c-line-1013/mainline/C-t3-anchor-b3c8041-20261014/t3-anchor/raw_answers.jsonl",
    "T4 (20261013T113900Z-1fcaf42d)":
        "eval/reports/c-line-1013/mainline/C-review-1013/t4-throttled/raw_answers.jsonl",
    "T5 (10-13 节流采集)":
        "eval/reports/c-line-1013/mainline/C-review-1013/t5-throttled/raw_answers.jsonl",
}

JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)


def join_count(sql: str) -> int:
    return len(JOIN_RE.findall(sql or ""))


def summarize(label: str, rel_path: str) -> None:
    path = ROOT / rel_path
    if not path.is_file():
        print(f"\n### {label}\n  缺失：{rel_path}")
        return
    total = 0
    with_sem = 0
    multi = 0
    multi_without_sem = 0
    offenders = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        total += 1
        resp = d.get("response") or {}
        tools = [seg.get("tool") for seg in (resp.get("trace") or [])]
        has_sem = "sql_semantic_validator" in tools
        sql = resp.get("sql") or ""
        jc = join_count(sql)
        if has_sem:
            with_sem += 1
        if jc >= 1:
            multi += 1
            if not has_sem:
                multi_without_sem += 1
                offenders.append((d.get("case_id"), jc, resp.get("data_source")))
    print(f"\n### {label}")
    print(f"  用例数 {total}｜trace 含 sql_semantic_validator 的用例 {with_sem}")
    print(f"  含 JOIN 的用例 {multi}｜其中**未经过语义校验** {multi_without_sem}")
    if offenders:
        print("  未校验的多表用例（前 15）："
              + ", ".join(f"{c}(JOIN×{n})" for c, n, _ in offenders[:15]))
        print(f"  合计 {len(offenders)} 个多表用例的跨表连接列从未被外键一致性校验覆盖")


def main() -> int:
    for label, rel in RUNS.items():
        summarize(label, rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())