"""S2 要求覆盖闸门 —— 最小隔离样本加载器。

把 ``schema.sql`` + ``seed.sql`` 灌进一个**独立库**，供 L2 层测试使用。

设计要点
--------
1. **采用允许名单（allowlist），不是黑名单**。2026-10-16 复核前的实现是黑名单，
   实测 13 个库名里有 6 个本该拒绝的（``adventureworks`` / ``ict_aw`` /
   ``scale_bench`` / ``verimind_accounts`` / 任意未知库名 / 中文库名）全都放行了。
   黑名单的语义是"没列进来的都放行"，它不是安全属性。而 ``schema.sql:14``
   第一句就是 ``DROP TABLE IF EXISTS`` —— 放行进别人的库，别人的表当场被删。
   所以改成允许名单 + **连库后的内容确认**两道闸门。

2. **幂等**：``schema.sql`` 自带 ``DROP TABLE IF EXISTS``，重复执行得到干净样本。

3. **只接受显式 DSN**，不从 ``.env`` 兜底。从环境变量兜底会让"我以为在样本库上跑"
   变成一件无法验证的事 —— 本项目的 T5 事故就是这么来的。

用法::

    & 'D:\\python\\python.exe' load.py --dsn 'postgresql://ict:...@127.0.0.1:5432/semantic_coverage_fixture'

    # 只检查 DSN 是否合法，不连库
    python load.py --dsn '...' --check-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SCHEMA_SQL = HERE / "schema.sql"
SEED_SQL = HERE / "seed.sql"
#: 只有这些库名允许被本加载器写入。
#:
#: **为什么是允许名单而不是黑名单**（这是 2026-10-16 复核后的修法）：
#: 黑名单的语义是「没列进来的都放行」，它不是安全属性 —— 实测 13 个库名里，
#: ``adventureworks`` / ``ict_aw`` / ``scale_bench`` / ``verimind_accounts`` /
#: 任意未知库名 / 中文库名 共 6 个**该拒的全都放行了**。
#: 而 ``schema.sql:14`` 第一行就是 ``DROP TABLE IF EXISTS``：
#: 一旦放行进别人的库，别人的表当场被删。
#: 所以改成**允许名单**：没写在这里的库名一律拒绝。
ALLOWED_DATABASES: dict[str, str] = {
    "semantic_coverage_fixture": "S2 要求覆盖闸门的隔离样本库（README.md 里的标准名字）。",
    "semantic_coverage_fixture_aw": "备用隔离库：需要同时开第二组样本时使用。",
}

#: 允许名单的通配前缀。测试临时库（pytest 起的临时库）也走这里。
ALLOWED_PREFIXES: tuple[str, ...] = ("semantic_coverage_",)

#: 即使库名在名单内，库里也**必须**已经有这五张"标记表"才肯写。
#: 理由：库名对了不代表库对了 —— 有人拿同名库干别的也是可能的。
#: SQLite/Postgres 都能创建同名库，所以名字不可信，**内容才可信**。
FIXTURE_TABLES: frozenset[str] = frozenset(
    {"customer", "invoice", "employee", "country_group", "test_document"}
)

FIXTURE_TABLES_SQL = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = current_schema() AND table_name = ANY(%s)
"""

#: 当前 schema 里的**所有**基表。用于判断这个库是不是"别人的库"。
ALL_BASE_TABLES_SQL = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
"""


def database_name(dsn: str) -> str:
    """从 DSN 里取库名。``postgresql://host:5432/name`` -> ``name``。"""
    path = urlsplit(dsn).path or ""
    return path.lstrip("/").split("?")[0]


def check_database_allowed(dsn: str) -> None:
    """库名不在允许名单里就 ``SystemExit``。

    这是**连库之前**的闸门：不给"手滑一下"的机会，也不给未知库任何机会。
    """
    name = database_name(dsn)
    if not name:
        raise SystemExit(
            "DSN 里没有库名，拒绝执行。本加载器必须显式指定隔离库，"
            "以免误落到默认库。"
        )
    if name in ALLOWED_DATABASES:
        return
    if name.startswith(ALLOWED_PREFIXES):
        return
    raise SystemExit(
        f"拒绝加载：库名 {name!r} 不在允许名单内。\n"
        f"本加载器采用**允许名单**（不是黑名单）—— 没列进来的库名一律拒绝，\n"
        f"因为 schema.sql 第一句就是 DROP TABLE IF EXISTS，放行进别人的库会删掉别人的表。\n"
        f"允许的库名：{sorted(ALLOWED_DATABASES)}\n"
        f"允许的前缀：{list(ALLOWED_PREFIXES)}\n"
        f"若确需新库，请先把库名加进 ALLOWED_DATABASES 并写明理由。"
    )


def reject_business_database(dsn: str) -> None:
    """向后兼容的别名。

    ``verify_expected.py`` 与早期文档都引了这个名字；行为已经从"黑名单"
    升级为"允许名单"，但维持同名以免调用方全部改一遍。
    """
    check_database_allowed(dsn)


def assert_fixture_database(conn) -> None:
    """连库之后的第二道闸门：确认目标库要么**是空的**、要么**已经是我们的样本库**。

    名字对了不代表库对了 —— 同名库被拿去干别的也是可能的。所以这里看内容：

    * 库里**没有任何表** → 放行（全新库，本来就要往里灌）；
    * 库里**恰好是我们这五张标记表** → 放行（幂等重跑）；
    * 库里有**别的业务表** → 拒绝。schema.sql 第一句是 ``DROP TABLE IF EXISTS``，
      放行进别人的库会把别人的表删掉。

    注意这里**不查行数**：行数由 ``load()`` 执行完再验。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        db = cur.fetchone()[0]
        cur.execute(FIXTURE_TABLES_SQL, (sorted(FIXTURE_TABLES),))
        found = {row[0] for row in cur.fetchall()}
        cur.execute(ALL_BASE_TABLES_SQL)
        all_tables = {row[0] for row in cur.fetchall()}

    if not all_tables:
        print(f"目标库 {db!r} 当前为空库，允许灌入。")
        return
    if all_tables <= FIXTURE_TABLES:
        print(f"库内标记表齐全：{', '.join(sorted(found))}（当前库 = {db!r}）")
        return
    foreign = sorted(all_tables - FIXTURE_TABLES)
    raise SystemExit(
        f"拒绝加载：库 {db!r} 里有 {len(foreign)} 张不属于本样本的表"
        f"（例如 {foreign[:5]}）。\n"
        f"库名对了不代表库对了：本样本只认 {sorted(FIXTURE_TABLES)} 这几张表，\n"
        f"且 schema.sql 第一句是 DROP TABLE IF EXISTS —— 继续下去会删掉这些表。\n"
        f"请换一个专用库名（前缀 semantic_coverage_）。"
    )


def load(dsn: str) -> None:
    """按 schema -> seed 顺序执行两个 SQL 文件。"""
    import psycopg  # 延迟导入：--check-only 不需要驱动

    for path in (SCHEMA_SQL, SEED_SQL):
        if not path.exists():
            raise SystemExit(f"样本文件缺失：{path}")

    with psycopg.connect(dsn) as conn:
        assert_fixture_database(conn)
        for path in (SCHEMA_SQL, SEED_SQL):
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
            print(f"OK  {path.name}  ({len(sql)} chars)")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM customer")
            customers = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM invoice")
            invoices = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM employee")
            employees = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM country_group")
            groups = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM test_document")
            documents = cur.fetchone()[0]

    print(
        "已加载："
        f"customer={customers} invoice={invoices} employee={employees} "
        f"country_group={groups} test_document={documents}"
    )
    expected = {"customer": 4, "invoice": 8, "employee": 3, "country_group": 4, "test_document": 2}
    actual = {
        "customer": customers,
        "invoice": invoices,
        "employee": employees,
        "country_group": groups,
        "test_document": documents,
    }
    if actual != expected:
        raise SystemExit(f"行数与规格不符：actual={actual} expected={expected}")
    print("行数与母方案 §7.1 规格一致。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", required=True, help="隔离库的 PostgreSQL DSN（必填，不从 .env 兜底）")
    parser.add_argument("--check-only", action="store_true", help="只校验 DSN 是否合法，不连库")
    args = parser.parse_args(argv)

    check_database_allowed(args.dsn)
    print(f"DSN 检查通过：目标库 = {database_name(args.dsn)!r}（在允许名单内）")

    if args.check_only:
        return 0

    load(args.dsn)
    return 0


if __name__ == "__main__":
    sys.exit(main())