#!/usr/bin/env python
"""连上 PostgreSQL 导出 schema_meta.json —— 容器起来之后的权威口径。

与 gen_schema_meta_from_ddl.py 的关系：
    静态解析（无数据库）→ 先给 A 开工，字段说明靠人工维护文件补齐；
    本脚本（连数据库）    → 覆盖它。真相只有一份：以本脚本输出为准。
    容器起来后必须重跑一次，并用 diff 记录差异，差异写进 docs/。

用法：
    cp .env.example .env      # 填好 DATABASE_URL
    python infra/scripts/export_schema_meta.py
    python infra/scripts/export_schema_meta.py --database chinook --schemas public --samples 8
    python infra/scripts/export_schema_meta.py --dsn "postgresql://ict:pwd@127.0.0.1:5432/chinook"

    # 多 schema 库（AdventureWorks）：不逐个列 schema 名，直接扫全部业务 schema
    python infra/scripts/export_schema_meta.py \
        --dsn "postgresql://postgres:postgres@127.0.0.1:5433/postgres" \
        --database adventureworks --all-schemas \
        --out infra/schema_meta.adventureworks.json --descriptions none

依赖：psycopg（psycopg3）。未安装时脚本会给出安装命令而不是抛栈。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_meta_contract import (  # noqa: E402
    ColumnMeta,
    ForeignKeyMeta,
    ForeignKeyRef,
    SchemaMeta,
    TableMeta,
    now_utc_iso,
)

DEFAULT_DESC = Path(__file__).resolve().parents[1] / "schema_meta.descriptions.zh.json"

# .env 的查找顺序：仓库根，再 infra/。两处都读，已存在的环境变量优先（不覆盖）。
ENV_CANDIDATES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[1] / ".env",
)


def load_dotenv(candidates: tuple[Path, ...] = ENV_CANDIDATES) -> list[Path]:
    """把 .env 读进 os.environ，返回实际读到的文件列表。

    为什么需要这一步：README 的操作流程是 `cp .env.example .env` 之后直接跑本脚本，
    但 .env 不会被 shell 自动加载，`os.getenv("DATABASE_URL")` 拿到的是空，
    脚本会报「没有连接串」——文档写的流程与脚本实际行为不一致。
    这里补上读取，让文档里的流程真的能一条条走通。

    不引入 python-dotenv：本项目用到的格式只有 KEY=VALUE 与 # 注释两种，
    十几行就能覆盖；交付环境少一个第三方依赖也更省事。
    """
    loaded: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            # 已在环境里的不覆盖：命令行 export 的优先级高于文件
            if key and value and key not in os.environ:
                os.environ[key] = value
        loaded.append(path)
    return loaded


# 内部索引键统一用 (schema, 表名) 元组，不用裸表名——
# 多 schema 库下裸表名会互相覆盖，而且是静默少表（见 docs/CHANGELOG_schema_meta.md 1.1.0）。

ALL_SCHEMAS_SQL = """
SELECT nspname
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
ORDER BY nspname
"""

COLUMNS_SQL = """
SELECT c.table_schema,
       c.table_name,
       c.column_name,
       c.ordinal_position,
       c.data_type,
       c.character_maximum_length,
       c.numeric_precision,
       c.numeric_scale,
       c.is_nullable,
       c.column_default,
       -- 这里不用 SQL 层的 format(ident) 写法：psycopg3 在带参数执行时会扫描整条 SQL 的
       -- 参数占位符，连 SQL 注释里的百分号也照扫；而 format 的占位符（百分号-I）不是合法
       -- 参数占位符，会直接抛 ProgrammingError 拒绝执行。
       -- quote_ident() 语义与它相同（必要时才加双引号），且不含百分号，是此处最稳的写法。
       col_description((quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass::oid,
                       c.ordinal_position) AS comment
FROM information_schema.columns c
WHERE c.table_schema = ANY(%(schemas)s)
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

CONSTRAINTS_SQL = """
SELECT n.nspname AS table_schema,
       cl.relname AS table_name,
       con.conname AS constraint_name,
       con.contype AS constraint_type,
       (SELECT array_agg(a.attname ORDER BY x.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS x(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = x.attnum) AS columns,
       rn.nspname AS ref_schema,
       rcl.relname AS ref_table,
       (SELECT array_agg(a.attname ORDER BY x.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS x(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = x.attnum) AS ref_columns
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
LEFT JOIN pg_class rcl ON rcl.oid = con.confrelid
LEFT JOIN pg_namespace rn ON rn.oid = rcl.relnamespace
WHERE con.contype IN ('p', 'f')
  AND n.nspname = ANY(%(schemas)s)
ORDER BY n.nspname, cl.relname, con.conname
"""


def normalise_type(data_type: str, char_len: int | None, num_prec: int | None, num_scale: int | None) -> str:
    """把 information_schema 的类型摊平成与静态解析一致的写法，便于两份产物做 diff。

    静态解析拿到的是 `VARCHAR(160)` / `NUMERIC(10,2)`；
    information_schema 把它拆成 data_type + character_maximum_length / numeric_precision + numeric_scale，
    这里重新拼回去，否则两条路径的产无法直接比对。
    """
    t = (data_type or "").upper()
    if t in ("CHARACTER VARYING", "VARCHAR"):
        return f"VARCHAR({char_len})" if char_len else "VARCHAR"
    if t == "CHARACTER":
        return f"CHAR({char_len})" if char_len else "CHAR"
    if t == "NUMERIC" or t == "DECIMAL":
        if num_prec is not None and num_scale is not None:
            return f"NUMERIC({num_prec},{num_scale})"
        return "NUMERIC"
    if t == "TIMESTAMP WITHOUT TIME ZONE":
        return "TIMESTAMP"
    if t == "TIMESTAMP WITH TIME ZONE":
        return "TIMESTAMPTZ"
    if t == "DOUBLE PRECISION":
        return "DOUBLE PRECISION"
    if t == "INTEGER":
        return "INT"
    return t


def connect(dsn: str):
    try:
        import psycopg
        from psycopg import sql  # noqa: F401 —— 供调用方使用，此处仅确认可用
    except ModuleNotFoundError:
        raise SystemExit(
            "缺少 psycopg（psycopg3）。安装：\n"
            '    pip install "psycopg[binary]"\n'
            "注意不是 psycopg2。"
        )
    try:
        return psycopg.connect(dsn, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001 —— 这里要给的是可读提示，不是栈
        raise SystemExit(
            f"连接数据库失败：{exc}\n"
            "排查顺序：\n"
            "  1) 容器是否在跑： docker compose -f infra/docker-compose.yml ps\n"
            "  2) 健康检查是否通过：docker inspect --format '{{.State.Health.Status}}' ict-db\n"
            "  3) .env 里的 DATABASE_URL 库名是否为 chinook（不是 postgres）"
        )


def discover_schemas(conn) -> list[str]:
    """列出全部业务 schema。给压力库用：AdventureWorks 有 5 个 schema，逐个手写容易漏。"""
    with conn.cursor() as cur:
        cur.execute(ALL_SCHEMAS_SQL)
        return [r[0] for r in cur.fetchall()]


def fetch_schema(conn, schemas: list[str], samples: int, count_mode: str) -> tuple[dict, dict, list[tuple[str, str]]]:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(COLUMNS_SQL, {"schemas": schemas})
        col_rows = cur.fetchall()

        cur.execute(CONSTRAINTS_SQL, {"schemas": schemas})
        con_rows = cur.fetchall()

    columns: dict[tuple[str, str], list[dict]] = {}
    table_order: list[tuple[str, str]] = []
    for t_schema, table, col, pos, dtype, clen, prec, scale, nullable, default, comment in col_rows:
        key = (t_schema, table)
        if key not in columns:
            columns[key] = []
            table_order.append(key)
        columns[key].append(
            {
                "name": col,
                "type": normalise_type(dtype, clen, prec, scale),
                "nullable": nullable == "YES",
                "is_primary_key": False,
                "description": (comment or "").strip(),
                "sample_values": [],
                "ordinal": pos,
            }
        )

    pks: dict[tuple[str, str], list[str]] = {}
    fks: dict[tuple[str, str], list[ForeignKeyMeta]] = {}
    for t_schema, table, _cname, ctype, cols, ref_schema, ref_table, ref_cols in con_rows:
        key = (t_schema, table)
        cols = list(cols or [])
        if ctype == "p":
            pks[key] = cols
            for c in columns.get(key, []):
                if c["name"] in cols:
                    c["is_primary_key"] = True
                    c["nullable"] = False
        elif ctype == "f" and ref_table:
            fks.setdefault(key, []).append(
                ForeignKeyMeta(
                    columns=cols,
                    references=ForeignKeyRef(
                        schema=ref_schema or "public",
                        table=ref_table,
                        columns=list(ref_cols or []),
                    ),
                )
            )

    row_counts: dict[tuple[str, str], int | None] = {}
    with conn.cursor() as cur:
        for t_schema, table in table_order:
            if count_mode == "estimate":
                cur.execute(
                    "SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = %s AND n.nspname = %s",
                    (table, t_schema),
                )
                row = cur.fetchone()
                row_counts[(t_schema, table)] = int(row[0]) if row and row[0] is not None and row[0] >= 0 else None
            else:
                # 表名来自 pg 元数据，但仍必须走 Identifier 转义——不用字符串拼接拼标识符。
                # 这里必须同时给 schema，否则多 schema 同名表会数错（v1.1.0 修复）。
                cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t_schema, table)))
                row_counts[(t_schema, table)] = int(cur.fetchone()[0])

    sample_values: dict[tuple[str, str], dict[str, list[str]]] = {}
    if samples > 0:
        with conn.cursor() as cur:
            for key in table_order:
                t_schema, table = key
                for c in columns[key]:
                    try:
                        cur.execute(
                            sql.SQL("SELECT DISTINCT {} FROM {} WHERE {} IS NOT NULL LIMIT %s").format(
                                sql.Identifier(c["name"]),
                                sql.Identifier(t_schema, table),
                                sql.Identifier(c["name"]),
                            ),
                            (samples,),
                        )
                        sample_values.setdefault(key, {})[c["name"]] = [
                            ("" if v is None else str(v)) for (v,) in cur.fetchall()
                        ]
                    except Exception:  # noqa: BLE001 —— 个别列（如 vector）取不了样例，不该拖垮整次导出
                        conn.rollback()
                        sample_values.setdefault(key, {})[c["name"]] = []

    return columns, {"pks": pks, "fks": fks, "row_counts": row_counts, "samples": sample_values}, table_order


def load_descriptions(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {"tables": {}, "columns": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {"tables": raw.get("tables", {}), "columns": raw.get("columns", {})}


def _lookup(mapping: dict[str, str], t_schema: str, table: str, column: str | None = None) -> str:
    """说明文件取键：先试限定名，再退回裸表名。

    退回是为了让 v1.0.0 时代写的 Chinook 说明文件（键形如 `album.title`）
    不用改一个字就能继续用；新增的多 schema 说明则用 `Sales.SalesOrderHeader.TotalDue`
    这种限定键，避免同名表撞车。
    """
    suffix = f".{column}" if column else ""
    for candidate in (f"{t_schema}.{table}{suffix}", f"{table}{suffix}"):
        if candidate in mapping:
            return mapping[candidate]
    return ""


def main() -> int:
    env_files = load_dotenv()
    ap = argparse.ArgumentParser(description="从 PostgreSQL 导出 schema_meta.json")
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="连接串；默认读环境变量 DATABASE_URL（含 .env）")
    ap.add_argument("--database", default=None, help="写入 JSON 的库名；默认取 DSN 中的库名")
    ap.add_argument("--schemas", nargs="+", default=["public"], help="要导出的 schema，默认 public")
    ap.add_argument(
        "--all-schemas",
        action="store_true",
        help="导出全部业务 schema（排除 pg_catalog / information_schema / toast）。多 schema 库用这个，免得漏",
    )
    ap.add_argument("--samples", type=int, default=8, help="每列样例值个数；0 表示不取")
    ap.add_argument("--count-mode", choices=["exact", "estimate"], default="exact")
    ap.add_argument("--descriptions", default=str(DEFAULT_DESC), help="中文说明文件；传 none 表示不用")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "schema_meta.json"))
    args = ap.parse_args()

    if not args.dsn:
        raise SystemExit("没有连接串。请 cp .env.example .env 并填 DATABASE_URL，或用 --dsn 传入。")

    database = args.database or args.dsn.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    desc_path = None if str(args.descriptions).lower() == "none" else Path(args.descriptions)
    desc = load_descriptions(desc_path)

    conn = connect(args.dsn)
    try:
        schemas = discover_schemas(conn) if args.all_schemas else list(args.schemas)
        if not schemas:
            raise SystemExit("没有发现任何业务 schema，请检查连接串指向的库是否正确。")
        columns, extra, table_order = fetch_schema(conn, schemas, args.samples, args.count_mode)
    finally:
        conn.close()

    tables: list[TableMeta] = []
    for t_schema, table in table_order:
        key = (t_schema, table)
        col_metas = []
        for c in columns[key]:
            col_metas.append(
                ColumnMeta(
                    name=c["name"],
                    type=c["type"],
                    nullable=c["nullable"],
                    is_primary_key=c["is_primary_key"],
                    # 数据库里已经写了 COMMENT 就用数据库的；没写就落到人工维护文件
                    description=c["description"] or _lookup(desc["columns"], t_schema, table, c["name"]),
                    sample_values=extra["samples"].get(key, {}).get(c["name"], []),
                )
            )
        tables.append(
            TableMeta(
                table=table,
                schema=t_schema,
                description=_lookup(desc["tables"], t_schema, table),
                row_count=extra["row_counts"].get(key),
                primary_key=extra["pks"].get(key, []),
                columns=col_metas,
                foreign_keys=extra["fks"].get(key, []),
            )
        )

    meta = SchemaMeta(
        database=database,
        dialect="postgres",
        source=f"live:{database}",
        generated_at=now_utc_iso(),
        generator="export_schema_meta.py (information_schema + pg_constraint)",
        tables=tables,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    stats = meta.stats()
    print(f"环境文件   : {', '.join(str(p) for p in env_files) if env_files else '(未找到 .env，改用环境变量 / --dsn)'}")
    print(f"库         : {database}  schema={schemas}")
    print(f"输出       : {out}")
    print(f"契约版本   : {meta.contract_version}")
    print(f"契约校验   : SchemaMeta 通过（含限定名唯一性）")
    print(f"schema / 表 / 字段 / 外键 : {stats['schemas']} / {stats['tables']} / {stats['columns']} / {stats['foreign_keys']}")
    print(f"行数口径   : {args.count_mode}")
    print("-" * 78)
    for t in meta.tables:
        print(f"{t.qualified_name():<36} 字段={len(t.columns):<3} 行数={t.row_count}  主键={t.primary_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
