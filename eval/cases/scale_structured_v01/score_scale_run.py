"""离线复核结构化规模测试的实际 SQL、结果和端到端耗时。

采集器只读 questions.jsonl；本脚本仅在原始回答冻结后读取 references.jsonl。
所有复算查询都在 PostgreSQL 只读事务中执行，并限制在本测试的三张规模表内。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra" / "scripts"))
from export_schema_meta import load_dotenv  # noqa: E402

sys.path.insert(0, str(ROOT / "eval" / "mainline"))
from score_t4_development import _executed_sql, _row_counter  # noqa: E402

PACK_DIR = Path(__file__).resolve().parent
TIERS = {"sales_scale_1000": 1_000, "sales_scale_10000": 10_000, "sales_scale_100000": 100_000}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number} 必须是 JSON 对象")
        rows.append(value)
    return rows


def linear_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)


def table_for_id(case_id: str) -> tuple[str, int]:
    prefix = case_id.split("-Q", 1)[0]
    mapping = {"S1K": ("sales_scale_1000", 1_000), "S10K": ("sales_scale_10000", 10_000), "S100K": ("sales_scale_100000", 100_000)}
    if prefix not in mapping:
        raise ValueError(f"未知测试题编号：{case_id}")
    return mapping[prefix]


def validate_select(statement: str, expected_table: str) -> None:
    parsed = sqlglot.parse(statement, read="postgres")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
        raise ValueError("只允许单条 SELECT 语句")
    tables = list(parsed[0].find_all(exp.Table))
    names = {table.name.casefold() for table in tables}
    schemas = {str(table.db or "").casefold() for table in tables}
    if names != {expected_table.casefold()} or schemas != {"scale"}:
        raise ValueError("评分 SQL 引用了测试档位之外的表")


def query_read_only(connection: psycopg.Connection, statement: str, expected_table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    validate_select(statement, expected_table)
    with connection.cursor() as cursor:
        cursor.execute(statement)
        if cursor.description is None:
            raise ValueError("SELECT 未返回列")
        columns = [description.name for description in cursor.description]
        return columns, cursor.fetchall()


def semantic_rows(case_id: str, columns: list[str], rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """For monthly aggregation only, compare calendar month, not date-vs-text encoding."""
    if not case_id.endswith("-Q08") or "month" not in columns:
        return rows
    month_index = columns.index("month")
    normalized = []
    for row in rows:
        values = list(row)
        value = values[month_index]
        if isinstance(value, (date, datetime)):
            values[month_index] = value.strftime("%Y-%m")
        else:
            values[month_index] = str(value)[:7]
        normalized.append(tuple(values))
    return normalized


def data_fingerprint(connection: psycopg.Connection, manifest: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for item in manifest["tables"]:
        name = str(item["name"])
        if name not in TIERS:
            raise ValueError(f"清单含非预期表：{name}")
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*), md5(string_agg(sale_id || '|' || sale_date || '|' || region || '|' || product_id || '|' || quantity || '|' || unit_price || '|' || amount, E'\\n' ORDER BY sale_id)) FROM scale.{name}"
            )
            row_count, content_md5 = cursor.fetchone()
        matches = row_count == item["row_count"] and content_md5 == item["content_md5"]
        checks.append({"table": name, "row_count": row_count, "content_md5": content_md5, "matches_manifest": matches})
    return {"verified": all(item["matches_manifest"] for item in checks), "tables": checks}


def score_run(
    run_dir: Path,
    questions_path: Path,
    references_path: Path,
    manifest_path: Path,
    runtime_manifest_path: Path,
    dsn: str,
) -> dict[str, Any]:
    raw_path = run_dir / "raw_answers.jsonl"
    freeze_path = run_dir / "freeze.json"
    if not raw_path.is_file() or not freeze_path.is_file():
        raise FileNotFoundError("运行目录缺少 raw_answers.jsonl 或 freeze.json")
    output_path = run_dir / "structured_scale_score.json"
    if output_path.exists():
        raise FileExistsError(f"拒绝覆盖已有评分：{output_path}")

    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    questions = read_jsonl(questions_path)
    references = read_jsonl(references_path)
    raw_rows = read_jsonl(raw_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = [str(item["id"]) for item in questions]
    if freeze.get("capture_status") != "completed" or freeze.get("answer_key_read_by_driver") is not False:
        raise ValueError("采集未完成或答案隔离标记不合格")
    if freeze.get("case_file_sha256") != sha256_file(questions_path):
        raise ValueError("冻结记录与当前问题包哈希不一致")
    for freeze_key, artifact_path in (
        ("data_manifest", manifest_path),
        ("service_runtime_manifest", runtime_manifest_path),
    ):
        recorded = freeze.get(freeze_key)
        if not isinstance(recorded, dict) or recorded.get("sha256") != sha256_file(artifact_path):
            raise ValueError(f"冻结记录中的 {freeze_key} 哈希与当前文件不一致")
    if len(ids) != len(set(ids)) or set(ids) != {str(item["id"]) for item in references}:
        raise ValueError("题目与参考查询编号不一致")
    if len(raw_rows) != len(ids) or {str(item["case_id"]) for item in raw_rows} != set(ids):
        raise ValueError("本评分器要求每题一条响应；运行记录缺失或重复")
    raw_by_id = {str(item["case_id"]): item for item in raw_rows}
    ref_by_id = {str(item["id"]): item for item in references}

    load_dotenv()
    cases: list[dict[str, Any]] = []
    by_tier: dict[str, list[dict[str, Any]]] = {name: [] for name in TIERS}
    with psycopg.connect(dsn, connect_timeout=8, options="-c statement_timeout=10000") as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        fingerprint = data_fingerprint(connection, manifest)
        if not fingerprint["verified"]:
            raise RuntimeError("规模表指纹与数据清单不一致，停止评分")
        database = connection.execute("SELECT current_database()").fetchone()[0]
        if database != manifest.get("database_name"):
            raise RuntimeError(
                "当前评分数据库与数据清单不一致："
                f"清单要求 {manifest.get('database_name')!r}，实际连到 {database!r}。"
                "三档规模表已迁出 Chinook，请把 DATABASE_URL（或 --dsn-env 指定的变量）"
                "指向 scale_bench 后再评分。"
            )

        for case_id in ids:
            record = raw_by_id[case_id]
            response = record.get("response") if isinstance(record.get("response"), dict) else {}
            table, row_tier = table_for_id(case_id)
            executed, sql_text = _executed_sql(response)
            row: dict[str, Any] = {
                "id": case_id,
                "table": f"scale.{table}",
                "row_tier": row_tier,
                "http_status": record.get("http_status"),
                "request_error": (record.get("error") or {}).get("type") if isinstance(record.get("error"), dict) else record.get("error"),
                "elapsed_ms": record.get("elapsed_ms"),
                "sql_executed": executed,
                "exact_result_match": False,
                "semantic_result_match": False,
                "columns_match": False,
                "answer_rows": None,
                "reference_rows": None,
                "error": None,
            }
            if executed and sql_text:
                try:
                    with connection.transaction():
                        actual_columns, actual_rows = query_read_only(connection, sql_text, table)
                        expected_columns, expected_rows = query_read_only(connection, str(ref_by_id[case_id]["sql"]), table)
                        row.update({
                            "columns_match": actual_columns == expected_columns,
                            "exact_result_match": actual_columns == expected_columns and _row_counter(actual_rows) == _row_counter(expected_rows),
                            "semantic_result_match": (
                                actual_columns == expected_columns
                                and _row_counter(semantic_rows(case_id, actual_columns, actual_rows))
                                == _row_counter(semantic_rows(case_id, expected_columns, expected_rows))
                            ),
                            "answer_rows": len(actual_rows),
                            "reference_rows": len(expected_rows),
                        })
                except Exception as exc:  # preserve the case while keeping the connection usable
                    row["error"] = type(exc).__name__
            else:
                row["error"] = "no_successful_sql_execution_trace"
            cases.append(row)
            by_tier[table].append(row)

    latency_summary: dict[str, Any] = {}
    for table, rows in by_tier.items():
        valid_latencies = [
            float(row["elapsed_ms"])
            for row in rows
            if row["http_status"] == 200 and row["request_error"] is None and row["sql_executed"] and isinstance(row["elapsed_ms"], (int, float))
        ]
        latency_summary[table] = {
            "attempted": len(rows),
            "http_200": sum(row["http_status"] == 200 for row in rows),
            "sql_executed": sum(row["sql_executed"] for row in rows),
            "exact_matches": sum(row["exact_result_match"] for row in rows),
            "semantic_matches": sum(row["semantic_result_match"] for row in rows),
            "successful_latency_count": len(valid_latencies),
            "successful_latency_ms": {
                "p50": linear_percentile(valid_latencies, 0.50),
                "p95": linear_percentile(valid_latencies, 0.95),
                "max": round(max(valid_latencies), 2) if valid_latencies else None,
            },
            "all_request_latency_ms": {
                "p50": linear_percentile([float(row["elapsed_ms"]) for row in rows if isinstance(row["elapsed_ms"], (int, float))], 0.50),
                "p95": linear_percentile([float(row["elapsed_ms"]) for row in rows if isinstance(row["elapsed_ms"], (int, float))], 0.95),
                "max": max((float(row["elapsed_ms"]) for row in rows if isinstance(row["elapsed_ms"], (int, float))), default=None),
            },
        }

    result = {
        "schema": "structured_scale_run_score_v1",
        "run_id": freeze.get("run_id"),
        "scored_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_type": "exposed_synthetic_efficiency_development_test_not_real_business_evaluation",
        "database": database,
        "data_fingerprint": fingerprint,
        "question_sha256": sha256_file(questions_path),
        "reference_sha256": sha256_file(references_path),
        "manifest_sha256": sha256_file(manifest_path),
        "runtime_manifest_sha256": sha256_file(runtime_manifest_path),
        "raw_answers_sha256": sha256_file(raw_path),
        "freeze_sha256": sha256_file(freeze_path),
        "case_count": len(cases),
        "executed_sql_count": sum(row["sql_executed"] for row in cases),
        "exact_matches": sum(row["exact_result_match"] for row in cases),
        "semantic_matches": sum(row["semantic_result_match"] for row in cases),
        "http_errors": sum(row["http_status"] != 200 for row in cases),
        "request_errors": sum(row["request_error"] is not None for row in cases),
        "latency_definition": "client-observed /api/ask wall time; successful-latency statistics require HTTP 200, no collector error, and a successful SQL execution trace; all-request statistics are also reported; p50/p95 use linear interpolation",
        "latency_summary_by_table": latency_summary,
        "index_timing": "not measured by this scorer; report index rebuild duration separately, not as cold OS/database cache latency",
        "limitations": [
            "固定合成数据仅用于结构化查询效率观察，不代表真实业务数据效果。",
            "同一测试机与数据库环境下的端到端耗时，不应跨硬件直接比较。",
            "数据库缓冲缓存不会因重启 API 服务而清空；轮次重启服务不等于冷数据库缓存。",
            "结果匹配由冻结 SQL 与参考 SQL 在同一只读数据库上复算，衡量查询结果，不是自然语言答案的人工质量评分。",
            "严格匹配同时要求列名、值和类型一致；语义匹配另对 Q08 的月份键统一为 YYYY-MM，仅消除日期值与月份文本的等价表示差异。",
        ],
        "cases": cases,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--questions", type=Path, default=PACK_DIR / "questions.jsonl")
    parser.add_argument("--references", type=Path, default=PACK_DIR / "references.jsonl")
    parser.add_argument("--manifest", type=Path, default=PACK_DIR / "data_manifest.json")
    parser.add_argument("--runtime-manifest", type=Path, default=PACK_DIR / "runtime_manifest.json")
    parser.add_argument("--dsn-env", default="DATABASE_URL", help="只读评分数据库连接对应的环境变量名；不打印连接值")
    args = parser.parse_args()

    load_dotenv()
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        raise SystemExit(f"缺少环境变量 {args.dsn_env}；连接值不会写入评分文件")
    result = score_run(
        args.run_dir.resolve(), args.questions.resolve(), args.references.resolve(),
        args.manifest.resolve(), args.runtime_manifest.resolve(), dsn,
    )
    print(f"结构化规模评分：严格匹配 {result['exact_matches']}/{result['case_count']}；语义匹配 {result['semantic_matches']}/{result['case_count']}；实际执行 SQL {result['executed_sql_count']}/{result['case_count']}")
    for table, summary in result["latency_summary_by_table"].items():
        print(f"{table}: 严格 {summary['exact_matches']}/{summary['attempted']}，语义 {summary['semantic_matches']}/{summary['attempted']}，成功耗时样本 {summary['successful_latency_count']}，P50/P95/最大值={summary['successful_latency_ms']}")
    print(f"评分已写入 {args.run_dir.resolve() / 'structured_scale_score.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
