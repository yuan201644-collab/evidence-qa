"""对本评测独占的三张规模表分别计时：删除并重建 sale_date 索引。

这只测索引构建，不清理 PostgreSQL/操作系统缓存，不计表创建和灌数时间。
仅允许操作 scale.sales_scale_{1000,10000,100000} 对应的指定索引。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra" / "scripts"))
from export_schema_meta import load_dotenv  # noqa: E402

TIERS = (1_000, 10_000, 100_000)


def measure(dsn_env: str, rounds: int, output_path: Path) -> dict:
    load_dotenv()
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(f"缺少环境变量 {dsn_env}；连接值不会输出或写入文件")
    if output_path.exists():
        raise FileExistsError(f"拒绝覆盖已有索引计时文件：{output_path}")

    observations = []
    with psycopg.connect(dsn, connect_timeout=8) as connection:
        database = connection.execute("SELECT current_database()").fetchone()[0]
        if database != "chinook":
            raise RuntimeError("当前连接不是预期的 chinook 数据库，拒绝修改任何索引")
        # Complete all scope checks before the first DDL operation.
        for row_count in TIERS:
            table_name = f"sales_scale_{row_count}"
            index_name = f"sales_scale_{row_count}_sale_date_idx"
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM scale." + table_name)
                actual_count = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'scale' AND indexname = %s",
                    (index_name,),
                )
                index_row = cursor.fetchone()
            if actual_count != row_count or not index_row or "sale_date" not in index_row[0]:
                raise RuntimeError(f"scale.{table_name} 行数或专属日期索引不符，拒绝修改")
        connection.commit()

        for round_number in range(1, rounds + 1):
            # Rotate table order to reduce a fixed first/last-order effect.
            ordered_tiers = list(TIERS[round_number % len(TIERS):] + TIERS[:round_number % len(TIERS)])
            for row_count in ordered_tiers:
                table_name = f"sales_scale_{row_count}"
                index_name = f"sales_scale_{row_count}_sale_date_idx"
                with connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM scale." + table_name)
                    actual_count = cursor.fetchone()[0]
                    cursor.execute(sql.SQL("DROP INDEX IF EXISTS scale.{}").format(sql.Identifier(index_name)))
                connection.commit()

                started = time.perf_counter()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            sql.SQL("CREATE INDEX {} ON scale.{} (sale_date)").format(
                                sql.Identifier(index_name), sql.Identifier(table_name)
                            )
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    # Ensure the dedicated fixture's expected index exists after a failed timing attempt.
                    with connection.cursor() as cursor:
                        cursor.execute(
                            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON scale.{} (sale_date)").format(
                                sql.Identifier(index_name), sql.Identifier(table_name)
                            )
                        )
                    connection.commit()
                    raise
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                observations.append({
                    "round": round_number,
                    "table": f"scale.{table_name}",
                    "row_count": actual_count,
                    "index": index_name,
                    "index_build_ms": elapsed_ms,
                })

    result = {
        "schema": "structured_scale_index_build_v1",
        "database": database,
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "rounds": rounds,
        "method": "DROP only the named index owned by each scale fixture, then time CREATE INDEX on sale_date; data rows are not changed",
        "not_measured": "table creation, data loading, cache clearing, and cold OS/PostgreSQL cache latency",
        "observations": observations,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--dsn-env", default="DATABASE_URL")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds 必须大于 0")
    result = measure(args.dsn_env, args.rounds, args.output.resolve())
    print(f"索引构建计时完成：{len(result['observations'])} 条记录，文件 {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
