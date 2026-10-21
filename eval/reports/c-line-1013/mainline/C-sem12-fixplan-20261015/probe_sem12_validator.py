#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sem-12 根因实测：现有语义校验器能否拦住「EXCEPT 第二分支连接列错」。

只读实验：读 schema_meta.json 构造 JoinPlan，对三条 SQL 跑 SqlSemanticValidator.check()
  A. 参考 SQL（NOT IN 子查询）
  B. 候选 SQL（EXCEPT + 第二分支 ON c.customer_id = i.invoice_id）
  C. 候选 SQL 修正版（仅把该 ON 改为 = i.customer_id）

用途：为「最小修复方案」提供事实依据，不修改任何实现/判据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.nl2sql.planning_models import JoinEdge, JoinPlan  # noqa: E402
from app.nl2sql.semantic_validator import (  # noqa: E402
    SqlSemanticValidator,
    _immediate_nested_queries,
)
import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

GOLD = (
    "SELECT DISTINCT i.customer_id FROM invoice i "
    "JOIN invoice_line il ON il.invoice_id=i.invoice_id "
    "JOIN track t ON t.track_id=il.track_id "
    "JOIN genre g ON g.genre_id=t.genre_id "
    "WHERE g.name='Rock' AND i.customer_id NOT IN ("
    "SELECT i2.customer_id FROM invoice i2 "
    "JOIN invoice_line il2 ON il2.invoice_id=i2.invoice_id "
    "JOIN track t2 ON t2.track_id=il2.track_id "
    "JOIN genre g2 ON g2.genre_id=t2.genre_id WHERE g2.name='Jazz') "
    "ORDER BY i.customer_id"
)

PRED = (
    "SELECT DISTINCT c.customer_id FROM customer AS c "
    "JOIN invoice AS i ON c.customer_id = i.customer_id "
    "JOIN invoice_line AS il ON i.invoice_id = il.invoice_id "
    "JOIN track AS t ON il.track_id = t.track_id "
    "JOIN genre AS g ON t.genre_id = g.genre_id WHERE g.name = 'Rock' "
    "EXCEPT SELECT DISTINCT c.customer_id FROM customer AS c "
    "JOIN invoice AS i ON c.customer_id = i.invoice_id "
    "JOIN invoice_line AS il ON i.invoice_id = il.invoice_id "
    "JOIN track AS t ON il.track_id = t.track_id "
    "JOIN genre AS g ON t.genre_id = g.genre_id WHERE g.name = 'Jazz' "
    "ORDER BY customer_id LIMIT 1000"
)

PRED_FIXED = PRED.replace(
    "JOIN invoice AS i ON c.customer_id = i.invoice_id",
    "JOIN invoice AS i ON c.customer_id = i.customer_id",
)


def build_plan() -> JoinPlan:
    meta = json.loads((ROOT / "infra/schema_meta.json").read_text(encoding="utf-8"))
    edges = []
    tables = []
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
    return JoinPlan(target_grain="customer", tables=tuple(tables), edges=tuple(edges))


def probe(label: str, sql: str, plan: JoinPlan) -> None:
    parsed = sqlglot.parse_one(sql, read="postgres")
    nested = _immediate_nested_queries(parsed)
    print(f"\n===== {label} =====")
    print(f"  顶层节点类型      : {type(parsed).__name__}")
    print(f"  顶层 joins 数     : {len(list(parsed.args.get('joins') or ()))}")
    print(f"  捕获的嵌套 Query 数: {len(nested)}")
    for i, n in enumerate(nested, 1):
        kind = type(n.parent).__name__ if n.parent is not None else "?"
        tables = [t.sql(dialect='postgres') for t in n.find_all(exp.Table)]
        print(f"    nested#{i}: parent={kind} tables={tables}")
    res = SqlSemanticValidator().check(sql, plan, dialect="postgres", question="")
    print(f"  check() status    : {res.status}")
    print(f"  check() reason    : {res.reason_code}")
    print(f"  check() message   : {res.message}")
    print(f"  check() details   : {json.dumps(res.details, ensure_ascii=False, default=str)}")
    print(f"  >> 是否放行执行    : {res.status == 'pass'}")


def main() -> int:
    plan = build_plan()
    print(f"JoinPlan: tables={len(plan.tables)} edges={len(plan.edges)} "
          f"ambiguous={plan.ambiguous} warnings={plan.warnings}")
    probe("A 参考 SQL（NOT IN 子查询）", GOLD, plan)
    probe("B 候选 SQL（EXCEPT + 第二分支错列）", PRED, plan)
    probe("C 候选 SQL 仅修第二分支连接列", PRED_FIXED, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())