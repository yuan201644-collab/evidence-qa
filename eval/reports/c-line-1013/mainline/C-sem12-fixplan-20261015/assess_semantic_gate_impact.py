#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估「若把语义校验闸门打开」对既有开发回归 SQL 的影响（离线、只读）。

动机：sem-12 根因是语义校验默认关闭。打开闸门前必须先知道它会拦下什么。
本脚本把 T4/T5 既有回答里的候选 SQL 逐条喂给 SqlSemanticValidator，统计
status / reason_code 分布。

⚠️ 局限（必须随结论一起引用）：这里的 JoinPlan 是用 `infra/schema_meta.json`
   的**全部外键边**构造的，比运行时 JoinPlanner 针对单题生成的 plan **更宽松**，
   因此本结果是「闸门打开后拦截面」的**近似上界**，不是精确预测。
   真实拦截率需在开启后重跑在线回归确认。

不连数据库、不调用模型、不修改任何实现/判据/答案。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.nl2sql.planning_models import JoinEdge, JoinPlan  # noqa: E402
from app.nl2sql.semantic_validator import SqlSemanticValidator  # noqa: E402

RUNS = {
    "T4": "eval/reports/c-line-1013/mainline/C-review-1013/t4-throttled/raw_answers.jsonl",
    "T5": "eval/reports/c-line-1013/mainline/C-review-1013/t5-throttled/raw_answers.jsonl",
}
SCHEMAS = {
    "T4": "infra/schema_meta.adventureworks.json",
    "T5": "infra/schema_meta.json",
}
JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)


def build_plan(meta_rel: str) -> JoinPlan:
    meta = json.loads((ROOT / meta_rel).read_text(encoding="utf-8"))
    edges, tables = [], []
    for t in meta["tables"]:
        tables.append(f"{t['schema']}.{t['table']}".lower())
        for fk in t.get("foreign_keys") or ():
            ref = fk["references"]
            edges.append(JoinEdge(
                left_schema=t["schema"], left_table=t["table"],
                left_columns=tuple(fk["columns"]),
                right_schema=ref["schema"], right_table=ref["table"],
                right_columns=tuple(ref["columns"]),
                source="schema_meta",
            ))
    return JoinPlan(tables=tuple(tables), edges=tuple(edges))


def main() -> int:
    report = {}
    for run, rel in RUNS.items():
        plan = build_plan(SCHEMAS[run])
        validator = SqlSemanticValidator()
        statuses, reasons, rows = Counter(), Counter(), []
        multi = 0
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            resp = d.get("response") or {}
            sql = resp.get("sql") or ""
            if not sql or not JOIN_RE.search(sql):
                continue
            multi += 1
            try:
                res = validator.check(sql, plan, dialect="postgres", question="")
                status, reason = res.status, res.reason_code or "-"
            except Exception as exc:  # noqa: BLE001
                status, reason = "exception", type(exc).__name__
            statuses[status] += 1
            reasons[reason] += 1
            rows.append({
                "case_id": d.get("case_id"),
                "status": status,
                "reason_code": reason,
                "join_count": len(JOIN_RE.findall(sql)),
            })
        report[run] = {
            "schema_meta": SCHEMAS[run],
            "multi_table_cases": multi,
            "status": dict(statuses),
            "reason_codes": dict(reasons.most_common()),
            "cases": rows,
        }
        print(f"\n### {run}（schema_meta={SCHEMAS[run]}，plan 用全部外键边）")
        print(f"  多表用例 {multi} 条 → check() 状态分布：{dict(statuses)}")
        print("  reason_code 分布：")
        for k, v in reasons.most_common():
            print(f"    {k:<45} {v}")
        blocked = statuses.get("repair", 0) + statuses.get("unknown", 0) + statuses.get("exception", 0)
        print(f"  → 会被闸门拦下（repair/unknown/exception）：{blocked}/{multi}")

    out = ROOT / "eval/reports/c-line-1013/mainline/C-sem12-fixplan-20261015/impact_assessment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入：{out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())