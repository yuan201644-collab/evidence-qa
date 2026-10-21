#!/usr/bin/env python
"""结构化规模数据灌库 —— 为「系统效率」5 分生成 1 千 / 1 万 / 10 万三档同构业务表。

为什么放在独立 schema `scale`，而不是 public：
    `infra/scripts/verify_infra.py` 的「chinook 基表数」断言
    （`CHINOOK_EXPECT["tables"] = 11`）查的是 **public schema 下的全部基表**
    （`relkind='r' AND nspname='public'`）。规模表若落在 public，会把 11 顶成 12，
    验收脚本直接 FAIL。放进独立 schema 后，该断言与 healthcheck 的 `track = 3503`
    都无需改动 —— 这正是「选口径丙后验收断言不用改」得以成立的前提。

为什么是三张表，而不是「一张表反复重灌三档」：
    `infra/schema_meta.json` 的 `row_count` 是**单值**。A 登记后，verify_infra 的
    `check_meta_vs_live` 会拿元数据行数逐表比对真库。同一张表在不同档位行数不同，
    登记后必然在某一档失配；三张表各自行数固定，登记后始终一致。

幂等与确定性：
    - 幂等：DROP TABLE IF EXISTS → CREATE → INSERT，可重复执行，结果恒同。
    - 确定性：所有取值由 generate_series 的序号 g 纯算术派生，不含 random()/now()，
      同一 rows 参数在任意机器上得到逐字节相同的数据（内容 md5 可跨机复现）。
    - 用 `mod(g, n)` 而非 `g % n`：避免 SQL 文本里的 `%` 被驱动当参数占位符解析。

不触碰的东西：
    - 不 DROP 任何既有对象（只 CREATE SCHEMA / 建自己的表）；
    - 不改 `infra/initdb/`（`docker-entrypoint-initdb.d` 只在数据卷为空时执行一次，
      改它对已有数据卷无效，反而会制造「本地能跑、别人重起就变样」的假象）；
    - 不碰 Chinook 的 11 张表。

用法：
    .venv/Scripts/python.exe infra/scripts/seed_scale_sales.py
    .venv/Scripts/python.exe infra/scripts/seed_scale_sales.py --rows 1000
    .venv/Scripts/python.exe infra/scripts/seed_scale_sales.py --manifest data/corpus/scale/manifest_scale_db.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_schema_meta import load_dotenv  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = "scale"
DEFAULT_PREFIX = "sales_scale"
DEFAULT_ROWS = (1000, 10_000, 100_000)
DEFAULT_MANIFEST = REPO / "data" / "corpus" / "scale" / "manifest_scale_db.json"
#: 规模表的推荐落点：与 Chinook 同容器、不同库。
#:
#: 为什么不只靠独立 schema：`eval/mainline/fingerprint_postgres.py` 的指纹是**全库扫描**
#: （`table_schema NOT IN ('pg_catalog','information_schema')`），独立 schema 挡不住它 ——
#: 规模表一旦住在 chinook 里，Chinook 的内容指纹就会被改掉，T3/T5 的冻结校验随即失配。
#: `verify_infra.py` 用 11 表白名单所以没事，是**两套口径不一致**。
#: 放进独立库后，按 `DATABASE_URL` 扫 chinook 的指纹不再受影响。
DEFAULT_DATABASE = "scale_bench"

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

COLUMNS = [
    {"name": "sale_id", "type": "BIGINT", "note": "订单主键，等于生成序号 g（1..N）"},
    {"name": "sale_date", "type": "DATE", "note": "销售日期，在 2021-01-01~2025-12-31 区间内按行序均匀铺满（与档位无关，保证各档同一查询可比）"},
    {"name": "region", "type": "TEXT", "note": "销售大区，五种取值按 mod(g,5) 轮转"},
    {"name": "product_id", "type": "INTEGER", "note": "商品编号，1..100 按 mod(g,100)+1 轮转"},
    {"name": "quantity", "type": "INTEGER", "note": "数量，1..20"},
    {"name": "unit_price", "type": "NUMERIC(10,2)", "note": "单价，10.00~909.99 元，由 g 算术派生"},
    {"name": "amount", "type": "NUMERIC(12,2)", "note": "金额 = quantity * unit_price"},
]

CREATE_DDL = """
CREATE TABLE {table} (
    sale_id    BIGINT         PRIMARY KEY,
    sale_date  DATE           NOT NULL,
    region     TEXT           NOT NULL,
    product_id INTEGER        NOT NULL,
    quantity   INTEGER        NOT NULL,
    unit_price NUMERIC(10, 2) NOT NULL,
    amount     NUMERIC(12, 2) NOT NULL
)
"""

INDEX_DDL = "CREATE INDEX {idx} ON {table} (sale_date)"

INSERT_SQL = """
INSERT INTO {table} (sale_id, sale_date, region, product_id, quantity, unit_price, amount)
SELECT g,
       DATE '2021-01-01' + mod(g * 1826 / {rows}, 1826),
       (ARRAY['华东', '华北', '华南', '西南', '东北']::text[])[mod(g, 5) + 1],
       mod(g, 100) + 1,
       mod(g, 20) + 1,
       (mod(g * 37, 90000) + 1000)::numeric / 100,
       (mod(g, 20) + 1) * ((mod(g * 37, 90000) + 1000)::numeric / 100)
FROM generate_series(1, {rows}) AS g
"""

FINGERPRINT_SQL = """
SELECT count(*),
       COALESCE(sum(amount), 0)::text,
       min(amount)::text,
       max(amount)::text,
       min(sale_date)::text,
       max(sale_date)::text,
       min(sale_id),
       max(sale_id)
FROM {table}
"""

CONTENT_MD5_SQL = """
SELECT md5(string_agg(
           sale_id || '|' || sale_date || '|' || region || '|' || product_id || '|' ||
           quantity || '|' || unit_price || '|' || amount,
           E'\\n' ORDER BY sale_id))
FROM {table}
"""

REGION_COUNT_SQL = """
SELECT region, count(*) FROM {table} GROUP BY region ORDER BY region
"""


def _check_ident(value: str, label: str) -> str:
    if not _IDENT_RE.match(value):
        raise SystemExit(f"非法{label}：{value!r}（只允许小写字母/数字/下划线，且不以数字开头）")
    return value


def seed_one(conn, schema: str, prefix: str, rows: int) -> dict:
    """建表并灌入 rows 行，返回该档的指纹字典。"""
    from psycopg import sql

    tname = f"{prefix}_{rows}"
    tbl = sql.Identifier(schema, tname)

    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        # 幂等：先清后建。IF EXISTS 保证重复执行不报错。
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(tbl))
        cur.execute(sql.SQL(CREATE_DDL).format(table=tbl))
        cur.execute(sql.SQL(INSERT_SQL).format(table=tbl, rows=sql.Literal(rows)))
        # 索引在灌完数据后建（与真实建仓顺序一致），为「冷索引 vs 端到端」分开计时提供对象。
        # 注意：PG 的 CREATE INDEX 不接受 schema 限定的索引名，索引自动落在表所在 schema。
        idx_name = f"{prefix}_{rows}_sale_date_idx"
        cur.execute(sql.SQL(INDEX_DDL).format(idx=sql.Identifier(idx_name), table=tbl))

        cur.execute(sql.SQL(FINGERPRINT_SQL).format(table=tbl))
        cnt, sum_amount, min_amount, max_amount, min_date, max_date, min_id, max_id = cur.fetchone()

        cur.execute(sql.SQL(CONTENT_MD5_SQL).format(table=tbl))
        content_md5 = cur.fetchone()[0]

        cur.execute(sql.SQL(REGION_COUNT_SQL).format(table=tbl))
        region_counts = {r: c for r, c in cur.fetchall()}

    conn.commit()

    if cnt != rows:  # 灌库自校验：绝不让「说灌了 10 万行」而实际没灌的事静默发生
        raise SystemExit(f"{schema}.{tname} 行数不符：期望 {rows}，实测 {cnt}")

    ddl_text = CREATE_DDL.format(table=f"{schema}.{tname}").strip()
    idx_text = INDEX_DDL.format(idx=f"{schema}.{idx_name}", table=f"{schema}.{tname}")
    return {
        "rows": rows,
        "table": f"{schema}.{tname}",
        "ddl": ddl_text,
        "indexes": [idx_text],
        "columns": COLUMNS,
        "fingerprint": {
            "row_count": cnt,
            "content_md5": content_md5,
            "sum_amount": sum_amount,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "min_sale_date": min_date,
            "max_sale_date": max_date,
            "min_sale_id": min_id,
            "max_sale_id": max_id,
            "region_counts": region_counts,
        },
    }


def _ensure_database(dsn: str, database: str) -> str:
    """把 DSN 的库名换成 `database`，库不存在则建。返回新的 DSN。

    `CREATE DATABASE` 不能在目标库自己的会话里执行，所以先连到同实例的
    `postgres` 维护库上建，再返回指向新库的 DSN。

    为什么连 `postgres` 而不是原 DSN 的库：原 DSN 可能已经指向一个不存在的库
    （这正是要建它的情形），连不上就没法建。
    """
    from urllib.parse import urlsplit, urlunsplit

    import psycopg

    database = _check_ident(database, "库名")
    parts = urlsplit(dsn)

    def _with_db(name: str) -> str:
        return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))

    admin_dsn = _with_db("postgres")
    with psycopg.connect(admin_dsn, connect_timeout=8, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not exists:
            from psycopg import sql

            # 库名已过 _check_ident，这里再用 Identifier 兜一层，避免拼接注入。
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            print(f"  已建库  : {database}")
    target = _with_db(database)
    # 连一次确认真的可写可用，别把「建了但连不上」留到灌库阶段才暴露。
    with psycopg.connect(target, connect_timeout=8) as conn:
        conn.execute("SELECT 1")
    return target


def main() -> int:
    env_files = load_dotenv()
    ap = argparse.ArgumentParser(description="结构化规模数据灌库（多规模性能对比底座）")
    ap.add_argument("--dsn", default=None, help="覆盖连接串；默认读 .env 的 DATABASE_URL")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA, help=f"目标 schema（默认 {DEFAULT_SCHEMA}，刻意不落 public）")
    ap.add_argument("--table-prefix", default=DEFAULT_PREFIX, help=f"表名前缀（默认 {DEFAULT_PREFIX}）")
    ap.add_argument(
        "--rows",
        type=int,
        action="append",
        default=None,
        help=f"行数档位，可重复；缺省为 {DEFAULT_ROWS}",
    )
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest 输出路径")
    ap.add_argument(
        "--database",
        default=None,
        help=(
            "目标库名。给出时按 --dsn/.env 的 DATABASE_URL 改写库名后连接，"
            "库不存在则自动建（须连到同实例的管理库执行 CREATE DATABASE）。"
            f"推荐 {DEFAULT_DATABASE}：与 Chinook 同容器、不同库，"
            "使 Chinook 的全库指纹不再被规模表改动。"
        ),
    )
    args = ap.parse_args()

    dsn = args.dsn or os.getenv("DATABASE_URL")
    rows_list = args.rows or list(DEFAULT_ROWS)
    schema = _check_ident(args.schema, "schema 名")
    prefix = _check_ident(args.table_prefix, "表名前缀")

    print("结构化规模灌库")
    print(f"  环境文件: {', '.join(str(p) for p in env_files) if env_files else '(未找到 .env)'}")
    print(f"  连接串  : {dsn or '(未配置 DATABASE_URL)'}")
    print(f"  schema  : {schema}（独立 schema，public 下 Chinook 基表保持 11）")
    print(f"  档位    : {rows_list}")
    print("-" * 78)

    if not dsn:
        print("FAIL  没有连接串。请 cp .env.example .env，或用 --dsn 传入。")
        return 1

    try:
        import psycopg
    except ImportError:
        print("FAIL  缺少 psycopg，请用仓库 .venv 的解释器运行：")
        print("      .venv/Scripts/python.exe infra/scripts/seed_scale_sales.py")
        return 1

    if args.database:
        try:
            dsn = _ensure_database(dsn, args.database)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  准备目标库失败：{type(exc).__name__}: {str(exc).splitlines()[0]}")
            return 1
        print(f"  目标库  : {args.database}")

    try:
        conn = psycopg.connect(dsn, connect_timeout=8)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  连库失败：{type(exc).__name__}: {str(exc).splitlines()[0]}")
        print("      容器启动： docker compose -f infra/docker-compose.yml up -d")
        return 1

    tiers: list[dict] = []
    try:
        for n in rows_list:
            print(f"  灌 {schema}.{prefix}_{n} ...", flush=True)
            tier = seed_one(conn, schema, prefix, n)
            tiers.append(tier)
            fp = tier["fingerprint"]
            print(
                f"    OK  行数 {fp['row_count']}  金额合计 {fp['sum_amount']}  "
                f"日期 {fp['min_sale_date']}~{fp['max_sale_date']}  内容md5 {fp['content_md5'][:12]}…"
            )
    finally:
        conn.close()

    # 连接参数脱敏：口令来自 .env，绝不写进会入库的 manifest。
    dsn_info = _parse_dsn(dsn)
    manifest = {
        "generator": "infra/scripts/seed_scale_sales.py",
        "purpose": "题面「系统效率」5 分：结构化数据多规模（1 千 / 1 万 / 10 万行）性能对比实测底座",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "connection": dsn_info,
        "schema": schema,
        "isolation": {
            "public_schema_untouched": True,
            "note": (
                "规模表置于独立 schema `scale`，public 下 Chinook 基表数保持 11，"
                "故 verify_infra.py 的 chinook 断言与 healthcheck 的 track=3503 均无需改动。"
            ),
        },
        "determinism": {
            "random_free": True,
            "note": "全部取值由 generate_series 序号纯算术派生，content_md5 可在任意机器复现。",
        },
        "tiers": tiers,
    }

    out = Path(args.manifest)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换：写 staging 临时文件再 replace，避免中断留下半截 JSON。
    staging = out.with_suffix(out.suffix + ".tmp")
    staging.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staging.replace(out)

    print("-" * 78)
    print(f"manifest 已写入 {out}")
    print(f"三档规模表就绪：{', '.join(t['table'] for t in tiers)}")
    return 0


def _parse_dsn(dsn: str) -> dict:
    """把 DSN 拆成可安全落盘的连接参数（口令替换为占位符）。"""
    from urllib.parse import urlparse

    u = urlparse(dsn)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 5432,
        "dbname": (u.path or "/").lstrip("/") or "chinook",
        "user": u.username or "ict",
        "password": "<由 .env 的 DATABASE_URL 运行时注入，不落盘>",
    }


if __name__ == "__main__":
    raise SystemExit(main())
