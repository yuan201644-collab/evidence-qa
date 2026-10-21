"""把独立合成样例装入明确命名的 PostgreSQL 演示数据库。

此脚本是一次性数据准备入口，不属于在线问答执行路径。目标数据库必须名为
cross_source_demo，且五张目标表都为空；脚本拒绝向其他库或已有数据的表写入。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "equipment",
        "structured/equipment.csv",
        ("equipment_id", "equipment_name", "customer_id", "site_name"),
    ),
    (
        "contracts",
        "structured/contracts.csv",
        ("contract_id", "customer_id", "effective_from", "effective_to", "status", "timezone"),
    ),
    (
        "contract_equipment",
        "structured/contract_equipment.csv",
        ("contract_id", "equipment_id", "valid_from", "valid_to", "mapping_basis"),
    ),
    (
        "work_orders",
        "structured/work_orders.csv",
        ("work_order_id", "equipment_id", "opened_at", "summary"),
    ),
    (
        "work_order_events",
        "structured/work_order_events.csv",
        ("work_order_id", "event_type", "event_at"),
    ),
)


def load_demo_data(data_root: Path = ROOT) -> dict[str, int]:
    """仅向空的 cross_source_demo 数据库建表并载入指定目录中的 CSV。"""
    dsn = os.environ.get("CROSS_SOURCE_DEMO_DSN", "").strip()
    if not dsn:
        raise RuntimeError("请先设置 CROSS_SOURCE_DEMO_DSN，指向独立 cross_source_demo 数据库")

    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("缺少 psycopg，请先按项目依赖执行 uv sync") from exc

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            database_name = cur.fetchone()[0]
            if database_name != "cross_source_demo":
                raise RuntimeError(
                    "目标库必须精确命名为 cross_source_demo；拒绝写入 " + repr(database_name)
                )

            schema_sql = (data_root / "schema.sql").read_text(encoding="utf-8")
            for statement in schema_sql.split(";"):
                if statement.strip():
                    cur.execute(statement)

            for table_name, _relative_path, _columns in TABLES:
                cur.execute(f"SELECT COUNT(*) FROM cross_source_demo.{table_name}")
                if cur.fetchone()[0] != 0:
                    raise RuntimeError(
                        f"目标表 cross_source_demo.{table_name} 非空；为避免覆盖，停止载入"
                    )

            for table_name, relative_path, columns in TABLES:
                csv_path = data_root / relative_path
                column_sql = ", ".join(columns)
                copy_sql = (
                    f"COPY cross_source_demo.{table_name} ({column_sql}) "
                    "FROM STDIN WITH (FORMAT CSV, HEADER TRUE)"
                )
                with csv_path.open("rb") as source, cur.copy(copy_sql) as copy:
                    while chunk := source.read(64 * 1024):
                        copy.write(chunk)
                cur.execute(f"SELECT COUNT(*) FROM cross_source_demo.{table_name}")
                counts[table_name] = int(cur.fetchone()[0])

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT,
                        help="包含 schema.sql、structured/ 和 documents/ 的数据目录")
    args = parser.parse_args()
    counts = load_demo_data(args.data_dir.resolve())
    print("已载入独立演示数据库：" + ", ".join(f"{name}={count}" for name, count in counts.items()))


if __name__ == "__main__":
    main()
