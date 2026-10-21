#!/usr/bin/env python
"""数据底座一键验收 —— 把「口头说跑通了」换成可复跑、可贴给评委的输出。

为什么需要它：
    `docker compose ps` 显示 healthy，只证明容器进程活着（本项目已把健康检查升级为
    引擎+数据双条件，已是较强信号），但它仍不检查：
      - schema_meta.json 与真库是否还一致（生成器改了、库改了，都可能脱节）
      - pgvector 扩展到底装没装、版本是多少
      - 压力库的对象数是否真的是 80（而不是连到了一个空壳库）
    这些是「演示前必须确认、事后无法回忆」的事实，写成脚本才能每次复跑。

判定原则（确定性，纯函数式比较，不做 AI 推断）：
    - 每项检查给出期望值与实测值，不匹配就是 FAIL，不写「基本一致」这类模糊结论
    - 任何一条查询报错都转成一条 FAIL，绝不向外抛栈——验收脚本本身崩掉，
      等于把「哪里不达标」变成「脚本坏了」，那是把排查工作甩回给使用者
    - 硬检查失败 → 退出码 1；压力库不可达 → SKIP（可选组件，不阻塞），
      加 --require-aw 时升级为硬检查（演示当天该用这个）
    - 退出码只有 0（全通过）与 1（有 FAIL）两种，可直接接进 CI

用法：
    python infra/scripts/verify_infra.py
    python infra/scripts/verify_infra.py --require-aw        # 演示前：压力库也必须通
    python infra/scripts/verify_infra.py --json out.json     # 同时落一份机器可读结果
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_schema_meta import ENV_CANDIDATES, load_dotenv  # noqa: E402
from schema_meta_contract import CONTRACT_VERSION, SchemaMeta  # noqa: E402

INFRA = Path(__file__).resolve().parents[1]
CHINOOK_META = INFRA / "schema_meta.json"
AW_META = INFRA / "schema_meta.adventureworks.json"

# 期望值集中在这里。改了数据库或换了镜像，只改这一处，检查逻辑不用动。
CHINOOK_EXPECT = {
    "meta_contract": CONTRACT_VERSION,
    "tables": 11,
    "columns": 64,
    "foreign_keys": 11,
    "track_rows": 3503,
}
# Only the canonical Chinook fixture tables belong to the 11-table/64-column
# database assertion. Application-owned tables (for example, RAG metadata)
# may coexist in public without changing the Chinook dataset itself.
CHINOOK_TABLES = (
    "album",
    "artist",
    "customer",
    "employee",
    "genre",
    "invoice",
    "invoice_line",
    "media_type",
    "playlist",
    "playlist_track",
    "track",
)
AW_EXPECT = {
    "objects": 80,
    "tables": 71,
    "views": 9,
    "person_person": 19972,
    "meta_contract": CONTRACT_VERSION,
    "meta_tables": 80,
    "meta_columns": 607,
    "meta_foreign_keys": 90,
}

# 业务 schema 的判定：排除系统目录与 toast 表。两库共用同一条口径。
AW_OBJ_SQL = """
SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True
    skipped: bool = False

    @property
    def label(self) -> str:
        return "SKIP" if self.skipped else ("PASS" if self.ok else "FAIL")


def cmp_check(name: str, expect, actual, required: bool = True) -> Check:
    """单值比较。相等才算通过——不设容差，容差是模糊结论的入口。"""
    return Check(name=name, ok=expect == actual, detail=f"期望 {expect!r}，实测 {actual!r}", required=required)


def _query(conn, sql_text, params=None):
    """执行查询，返回 (ok, rows) 或 (False, 一行错误文本)。

    为什么失败时必须 rollback：PostgreSQL 中一条语句报错会把整个事务置为 aborted，
    此后同一连接上的所有语句都只返回 "current transaction is aborted"。
    不回滚的话，一处失败会连带把后面每条检查都判成失败，报告就失真了
    ——报「全部有问题」和漏报一样不可信。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            return True, cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 —— 连接本身已坏时，回滚也会失败，不该再抛
            pass
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"


def _scalar(conn, sql_text, params=None):
    ok, rows = _query(conn, sql_text, params)
    if not ok:
        return False, rows
    return True, (rows[0][0] if rows else None)


def scalar_check(conn, name: str, sql_text, expect, required: bool = True, params=None) -> Check:
    ok, value = _scalar(conn, sql_text, params)
    if not ok:
        return Check(name, False, f"查询失败：{value}", required)
    return cmp_check(name, expect, value, required)


def present_check(conn, name: str, sql_text, required: bool = True) -> Check:
    """确认某项存在（非 NULL 且非空）。用于 pgvector 这类「有/没有」的检查。"""
    ok, value = _scalar(conn, sql_text)
    if not ok:
        return Check(name, False, f"查询失败：{value}", required)
    return Check(name, value is not None, f"实测 {value!r}" if value is not None else "未找到（对应 initdb 脚本可能没跑）", required)


def check_chinook(dsn: str) -> list[Check]:
    try:
        import psycopg

        conn = psycopg.connect(dsn, connect_timeout=8)
    except Exception as exc:  # noqa: BLE001
        return [Check("主库可连接", False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}")]

    checks: list[Check] = []
    try:
        checks.append(present_check(conn, "主库可连接", "SHOW server_version"))
        checks.append(present_check(conn, "pgvector 扩展已安装", "SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        checks.append(
            scalar_check(
                conn,
                "chinook 基表数",
                """
                SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r' AND n.nspname = 'public' AND c.relname = ANY(%s)
                """,
                CHINOOK_EXPECT["tables"],
                params=(list(CHINOOK_TABLES),),
            )
        )
        checks.append(scalar_check(conn, "chinook track 行数", "SELECT count(*) FROM track", CHINOOK_EXPECT["track_rows"]))
        checks.append(
            scalar_check(
                conn,
                "chinook 字段总数",
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ANY(%s)",
                CHINOOK_EXPECT["columns"],
                params=(list(CHINOOK_TABLES),),
            )
        )
        checks.append(
            scalar_check(
                conn,
                "chinook 外键数",
                """
                SELECT count(*) FROM pg_constraint con
                JOIN pg_class cl ON cl.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = cl.relnamespace
                WHERE con.contype = 'f' AND n.nspname = 'public' AND cl.relname = ANY(%s)
                """,
                CHINOOK_EXPECT["foreign_keys"],
                params=(list(CHINOOK_TABLES),),
            )
        )
        checks.extend(check_meta_vs_live(conn, CHINOOK_META, "chinook", CHINOOK_EXPECT["meta_contract"]))
    finally:
        conn.close()
    return checks


def check_meta_vs_live(conn, meta_path: Path, label: str, expect_contract: str) -> list[Check]:
    """最硬的一条：schema_meta.json 里的每一张表，行数与真库逐表比对。

    为什么值得跑 N 次 count：schema_meta 是交给 A 的接口产物，
    它一旦与真库脱节（换了 DDL、重跑了生成器、库被改过），
    A 的 Schema Linking 会基于错误元数据工作，而错误不会当场暴露。
    逐表比对是唯一能把它变成「当场红灯」的办法。
    """
    from psycopg import sql

    if not meta_path.exists():
        return [Check(f"{label} 元数据文件存在", False, f"找不到 {meta_path}")]

    try:
        meta = SchemaMeta.model_validate(json.loads(meta_path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return [Check(f"{label} 元数据通过契约校验", False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}")]

    checks = [
        Check(
            f"{label} 元数据通过契约校验",
            True,
            f"契约 {meta.contract_version}，{len(meta.schemas())} schema / {len(meta.tables)} 表",
        ),
        cmp_check(f"{label} 元数据契约版本", expect_contract, meta.contract_version),
    ]

    mismatches: list[str] = []
    errors: list[str] = []
    for t in meta.tables:
        ok, value = _scalar(conn, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t.schema, t.table)))
        if not ok:
            errors.append(f"{t.qualified_name()}: {value}")
        elif value != t.row_count:
            mismatches.append(f"{t.qualified_name()}: 元数据 {t.row_count} vs 真库 {value}")

    checks.append(
        Check(
            f"{label} 元数据行数与真库逐表一致",
            not mismatches and not errors,
            "全部一致"
            if not mismatches and not errors
            else f"不一致 {len(mismatches)} 张：{mismatches[:5]}；查询失败 {len(errors)} 张：{errors[:3]}",
        )
    )
    return checks


def check_adventureworks(dsn: str, required: bool) -> list[Check]:
    try:
        import psycopg

        conn = psycopg.connect(dsn, connect_timeout=8)
    except Exception as exc:  # noqa: BLE001
        # 压力库是可选组件：连不上只算 SKIP，除非演示前显式要求
        return [
            Check(
                "压力库可连接",
                False,
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}。"
                "启动： docker compose -f infra/docker-compose.aw.yml up -d",
                required=required,
                skipped=not required,
            )
        ]

    checks: list[Check] = []
    try:
        checks.append(present_check(conn, "压力库可连接", "SHOW server_version", required))
        checks.append(scalar_check(conn, "压力库对象数（表+视图）", AW_OBJ_SQL, AW_EXPECT["objects"], required))

        ok, rows = _query(
            conn,
            """
            SELECT c.relkind::text, count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'v')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            GROUP BY c.relkind
            """,
        )
        if not ok:
            checks.append(Check("压力库基表数 / 视图数", False, f"查询失败：{rows}", required))
        else:
            kinds = dict(rows)
            checks.append(cmp_check("压力库基表数", AW_EXPECT["tables"], kinds.get("r"), required))
            checks.append(cmp_check("压力库视图数", AW_EXPECT["views"], kinds.get("v"), required))

        checks.append(scalar_check(conn, "Person.Person 行数", 'SELECT count(*) FROM "Person"."Person"', AW_EXPECT["person_person"], required))
        checks.append(
            scalar_check(
                conn,
                "压力库外键数",
                """
                SELECT count(*) FROM pg_constraint con
                JOIN pg_class cl ON cl.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = cl.relnamespace
                WHERE con.contype = 'f' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                """,
                AW_EXPECT["meta_foreign_keys"],
                required,
            )
        )
        checks.extend(check_meta_vs_live(conn, AW_META, "压力库", AW_EXPECT["meta_contract"]))
        stats = None
        if AW_META.exists():
            try:
                meta = SchemaMeta.model_validate(json.loads(AW_META.read_text(encoding="utf-8")))
                stats = meta.stats()
            except Exception as exc:  # noqa: BLE001
                checks.append(Check("压力库元数据可解析", False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}", required))
        if stats:
            checks.append(cmp_check("压力库元数据表数", AW_EXPECT["meta_tables"], len(meta.tables), required))
            checks.append(cmp_check("压力库元数据字段数", AW_EXPECT["meta_columns"], stats["columns"], required))
            checks.append(cmp_check("压力库元数据外键数", AW_EXPECT["meta_foreign_keys"], stats["foreign_keys"], required))
    finally:
        conn.close()
    return checks


def main() -> int:
    env_files = load_dotenv()
    ap = argparse.ArgumentParser(description="数据底座一键验收")
    ap.add_argument("--dsn", default=None, help="覆盖主库连接串；默认读 .env 的 DATABASE_URL")
    ap.add_argument("--aw-dsn", default=None, help="覆盖压力库连接串；默认读 .env 的 ADVENTUREWORKS_URL")
    ap.add_argument("--require-aw", action="store_true", help="把压力库检查升级为硬检查（演示前用）")
    ap.add_argument("--json", default=None, help="把结果同时写到该路径（机器可读）")
    args = ap.parse_args()

    dsn = args.dsn or os.getenv("DATABASE_URL")
    aw_dsn = args.aw_dsn or os.getenv("ADVENTUREWORKS_URL")

    print("数据底座验收")
    print(f"  环境文件: {', '.join(str(p) for p in env_files) if env_files else '(未找到 .env)'}")
    print(f"  主库    : {'已配置（连接值已隐藏）' if dsn else '(未配置 DATABASE_URL)'}")
    print(f"  压力库  : {'已配置（连接值已隐藏）' if aw_dsn else '(未配置 ADVENTUREWORKS_URL)'}")
    print("-" * 78)

    if not dsn:
        print("FAIL  没有主库连接串。请 cp .env.example .env，或用 --dsn 传入。")
        return 1

    checks = check_chinook(dsn)
    if aw_dsn:
        checks.extend(check_adventureworks(aw_dsn, required=args.require_aw))
    else:
        checks.append(
            Check(
                "压力库可连接",
                False,
                "未配置 ADVENTUREWORKS_URL",
                required=args.require_aw,
                skipped=not args.require_aw,
            )
        )

    for c in checks:
        print(f"{c.label:<4} {c.name}{'' if c.required else '（可选）'}")
        print(f"       {c.detail}")
    print("-" * 78)

    failed = [c for c in checks if not c.ok and c.required]
    skipped = [c for c in checks if c.skipped]
    passed = [c for c in checks if c.ok]
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项，跳过 {len(skipped)} 项")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"checks": [asdict(c) | {"label": c.label} for c in checks], "failed": len(failed)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")

    if failed:
        print("\n验收未通过。逐条整改：")
        for c in failed:
            print(f"  - {c.name}：{c.detail}")
        return 1

    print("\n验收通过：数据底座与元数据一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
