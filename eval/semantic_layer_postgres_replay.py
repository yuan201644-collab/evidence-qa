"""冻结语义实验预测在真实 PostgreSQL 上重放；不再调用模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from semantic_layer_experiment import (
    CheckedExecutor, ROOT, check_cases, compare_resultsets, database_fingerprint,
    gold_sql_has_order_by, load_env_file, result_rows, write_json,
)
from app.nl2sql.schema import SchemaCatalog


def normalized_data(executor: CheckedExecutor, catalog: SchemaCatalog) -> dict[str, Any]:
    """跨执行方言比较完整数据；只归一日期文本及数值类型。"""
    tables = {}
    for name in catalog.table_names():
        table = catalog.get_table(name)
        columns = ", ".join('"' + col.name.replace('"', '""') + '"' for col in table.columns)
        result = executor.execute('SELECT ' + columns + ' FROM "' + name.replace('"', '""') + '" ORDER BY ' + columns)
        if result.error:
            raise RuntimeError(f"数据读取失败：{name}")
        rows = []
        for values in result_rows(result):
            normalized = []
            for column, value in zip(table.columns, values):
                if value is not None and any(t in column.type.upper() for t in ("DATE", "TIMESTAMP")):
                    value = datetime.fromisoformat(str(value)).isoformat()
                elif value is not None and any(t in column.type.upper() for t in ("NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE")):
                    value = float(value)
                normalized.append(value)
            rows.append(normalized)
        tables[name] = {"rows": len(rows), "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()}
    return tables


def replay(run: Path) -> None:
    catalog = SchemaCatalog.load()
    original = run / "records.jsonl"
    records = [json.loads(line) for line in original.read_text(encoding="utf-8").splitlines()]
    cases = [json.loads(line) for line in (run / "cases_snapshot.jsonl").read_text(encoding="utf-8").splitlines()]
    freeze = json.loads((run / "freeze.json").read_text(encoding="utf-8"))
    for name, digest in freeze["source_sha256"].items():
        snapshot = run / "source_snapshot" / name
        if not snapshot.exists() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"冻结源文件快照未通过校验：{name}")
    postgres = CheckedExecutor("postgres", catalog)
    sqlite = CheckedExecutor("sqlite", catalog)
    try:
        original_sqlite_data = database_fingerprint(sqlite, catalog)
        if original_sqlite_data != freeze["data"]:
            raise RuntimeError("SQLite原始数据已变更")
        pg_data = normalized_data(postgres, catalog)
        if pg_data != normalized_data(sqlite, catalog):
            raise RuntimeError("两数据库完整数据不一致，不能直接重放比较")
        gold, checks = check_cases(cases, postgres)
        by_id = {case["id"]: case for case in cases}
        results = []
        output = run / "postgres_replay"
        output.mkdir(exist_ok=False)
        for record in records:
            result = postgres.execute(record["sql"] or "")
            verdict = compare_resultsets(result_rows(gold[record["id"]]), result_rows(result),
                                         pred_error=result.error,
                                         order_matters=gold_sql_has_order_by(by_id[record["id"]]["gold_sql"]))
            row = {key: record[key] for key in ("id", "arm", "repeat", "suite", "sql")}
            row.update({"error": result.error, "verdict": verdict.to_dict(), "execution_ms": result.elapsed_ms,
                        "original_verdict": record["verdict"]})
            results.append(row)
            path = output / f"predictions-{row['suite']}-{row['arm']}-r{row['repeat']}.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({k: row[k] for k in ("id", "sql", "error")}, ensure_ascii=False) + "\n")
        summary = {}
        pairs = []
        for suite in ("regression", "heldout"):
            summary[suite] = {}
            for arm in ("baseline", "static_rules"):
                matching = [r for r in results if r["suite"] == suite and r["arm"] == arm]
                summary[suite][arm] = {"passed": sum(r["verdict"]["kind"] == "pass" for r in matching), "total": len(matching)}
            for repeat in sorted({r["repeat"] for r in records}):
                pair = {"suite": suite, "repeat": repeat, "wins": [], "regressions": [], "both_failed": []}
                for case in (c for c in cases if c["suite"] == suite):
                    matched = {r["arm"]: r["verdict"]["kind"] == "pass" for r in results if r["id"] == case["id"] and r["repeat"] == repeat}
                    if matched["static_rules"] and not matched["baseline"]:
                        pair["wins"].append(case["id"])
                    elif matched["baseline"] and not matched["static_rules"]:
                        pair["regressions"].append(case["id"])
                    elif not matched["baseline"] and not matched["static_rules"]:
                        pair["both_failed"].append(case["id"])
                pairs.append(pair)
        if normalized_data(postgres, catalog) != pg_data:
            raise RuntimeError("重放前后数据发生变更")
        payload = {"version": "0.1", "updated_at": "2026-10-02", "execution_dialect": "postgres",
                   "prediction_origin": "冻结的两轮真实模型输出；生成阶段执行器为SQLite；重放无新增模型调用",
                   "records_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                   "replay_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "data_equivalent": True, "data": pg_data, "summary": summary, "pairs": pairs, "checks": checks, "results": results}
        write_json(output / "summary.json", payload)
        print(json.dumps(summary, ensure_ascii=False))
        print("PostgreSQL重放完成：" + str(output), flush=True)
    finally:
        postgres.close()
        sqlite.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结预测的PostgreSQL只读重放")
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    if run.parent != (ROOT / "eval/reports").resolve() or not run.name.startswith("run-semantic-"):
        parser.error("只接受本项目静态语义实验报告目录")
    load_env_file()
    replay(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
