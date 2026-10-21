"""复核冻结预测中的 SQLite 日期转换问题，不重新调用模型或覆盖原始分数。"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from semantic_layer_experiment import (
    CheckedExecutor, ROOT, compare_resultsets, gold_sql_has_order_by, result_rows, write_json,
)
from app.nl2sql.schema import SchemaCatalog
from app.nl2sql.validator import validate_sql
import sqlglot
from sqlglot import exp


def sqlite_compatible(sql: str) -> tuple[str, list[str]]:
    """只修复已识别的月份截断和无时区时间字面量，未知情况保持原样。"""
    changes: list[str] = []

    def rewrite(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.TimestampTrunc) and node.args.get("unit"):
            if node.args["unit"].name.upper() == "MONTH" and not node.args.get("zone"):
                changes.append("DATE_TRUNC month -> SQLite strftime")
                return exp.Anonymous(this="STRFTIME", expressions=[
                    exp.Literal.string("%Y-%m-01 00:00:00"), node.this.transform(rewrite)])
        if isinstance(node, exp.Cast) and node.args.get("to"):
            if node.args["to"].this == exp.DataType.Type.TIMESTAMP and isinstance(node.this, exp.Literal) and node.this.is_string:
                value = node.this.this
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    changes.append("TIMESTAMP literal -> SQLite datetime")
                    return exp.Anonymous(this="DATETIME", expressions=[exp.Literal.string(value)])
        return node

    tree = sqlglot.parse_one(sql, read="postgres").transform(rewrite)
    return tree.sql(dialect="sqlite"), changes


def audit(run: Path) -> None:
    catalog = SchemaCatalog.load()
    executor = CheckedExecutor("sqlite", catalog)
    try:
        # 固定的日期算子控制样本，不来自标准答案，验证转换本身。
        controls = [
            ("SELECT date_trunc('month', TIMESTAMP '2023-03-17 12:34:56') AS d", "2023-03-01 00:00:00"),
            ("SELECT TIMESTAMP '2023-01-01' AS d", "2023-01-01 00:00:00"),
        ]
        for query, expected in controls:
            converted, _ = sqlite_compatible(query)
            assert executor.inner.conn.execute(converted).fetchone()[0] == expected
        cases = {c["id"]: c for c in (json.loads(line) for line in (run / "cases_snapshot.jsonl").read_text(encoding="utf-8").splitlines())}
        records = [json.loads(line) for line in (run / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        gold = {}
        for cid, case in cases.items():
            checked = validate_sql(case["gold_sql"], catalog, dialect="postgres", auto_limit=0)
            if not checked.ok:
                raise ValueError(f"标准查询校验失败：{cid}")
            converted, _ = sqlite_compatible(checked.sql)
            gold[cid] = executor.inner.conn.execute(converted).fetchall()
        results: list[dict[str, Any]] = []
        for record in records:
            cid = record["id"]
            row = {k: record[k] for k in ("id", "arm", "repeat", "suite")}
            row["raw_verdict"] = record["verdict"]
            checked = validate_sql(record["sql"] or "", catalog, dialect="postgres", auto_limit=0)
            error = None
            predicted = []
            changes = []
            if not checked.ok:
                error = checked.error
            else:
                try:
                    converted, changes = sqlite_compatible(checked.sql)
                    row["converted_sql"] = converted
                    predicted = executor.inner.conn.execute(converted).fetchall()
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
            row["compatibility_changes"] = changes
            row["audited_verdict"] = compare_resultsets(
                gold[cid], predicted, pred_error=error,
                order_matters=gold_sql_has_order_by(cases[cid]["gold_sql"])).to_dict()
            results.append(row)
        summary = {}
        for suite in ("regression", "heldout"):
            summary[suite] = {}
            for arm in ("baseline", "static_rules"):
                matching = [r for r in results if r["suite"] == suite and r["arm"] == arm]
                summary[suite][arm] = {"passed": sum(r["audited_verdict"]["kind"] == "pass" for r in matching), "total": len(matching)}
        payload = {"version": "0.1", "updated_at": "2026-10-02", "controls_passed": len(controls),
                   "scope": "冻结预测的SQLite日期兼容复核，未经PostgreSQL复验，不覆盖原始分数", "summary": summary,
                   "records_sha256": hashlib.sha256((run / "records.jsonl").read_bytes()).hexdigest(),
                   "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "results": results}
        write_json(run / "sqlite_compatibility_audit.json", payload)
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        executor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite日期转换诊断复核")
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    if run.parent != (ROOT / "eval/reports").resolve() or not run.name.startswith("run-semantic-"):
        parser.error("只接受本项目的静态语义实验报告目录")
    audit(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
