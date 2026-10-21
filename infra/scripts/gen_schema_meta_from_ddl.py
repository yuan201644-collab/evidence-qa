#!/usr/bin/env python
"""从官方建库脚本静态解析出 schema_meta.json —— 不依赖数据库、不依赖 Docker。

它存在的理由（这是本项目的一条硬约束，不是临时脚本）：
    C 的第一个节点「09-19 前交 schema_meta.json 给 A」不能等容器跑起来。
    官方 _PostgreSql.sql 是「DDL + 全量 INSERT」放在一个文件里的，
    所以表结构、外键、行数、样例值都能直接解析出来，
    A 拿到手就能开工写 Schema Linking，不必被 Docker 安装卡住。

覆盖顺序：容器起来后必须用 export_schema_meta.py 重新导一遍（live 为准），
本脚本的输出在 metadata 里会标 source=<脚本名>，一眼可辨是哪条路径产出的。

关于 schema（v1.1.0 起）：
    本脚本按「脚本里写了就用写的，没写就落 public」处理，与 PostgreSQL 解析
    未限定标识符时的行为一致。Chinook 的建库脚本是不限定的（`CREATE TABLE album`），
    所以全部落 public，与连库口径一致。若将来换成带 schema 的脚本，
    `CREATE TABLE Sales.X` / `REFERENCES Sales.Y` 里的限定名会被照实取出，
    保证静态路径产出的 schema 与连库路径对得上。

用法：
    python infra/scripts/gen_schema_meta_from_ddl.py
    python infra/scripts/gen_schema_meta_from_ddl.py --ddl infra/initdb/02_chinook.sql \\
        --out infra/schema_meta.json --database chinook --samples 8
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

# 允许以 `python infra/scripts/gen_schema_meta_from_ddl.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_meta_contract import (  # noqa: E402
    DEFAULT_SCHEMA,
    ColumnMeta,
    ForeignKeyMeta,
    ForeignKeyRef,
    SchemaMeta,
    TableMeta,
    now_utc_iso,
)

DIALECT = "postgres"
DEFAULT_DDL = Path(__file__).resolve().parents[1] / "initdb" / "02_chinook.sql"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "schema_meta.json"
DEFAULT_DESC = Path(__file__).resolve().parents[1] / "schema_meta.descriptions.zh.json"

# 内部索引键统一用 (schema, 表名) 元组。多 schema 脚本下裸表名会互相覆盖，
# 而且是静默少表——这类错误不会抛异常，只会让下游少拿到一张表，最难查。
Key = tuple[str, str]


def load_descriptions(path: Path | None) -> dict[str, dict[str, str]]:
    """读取人工维护的中文说明。

    为什么说明要单独一个文件：schema_meta.json 是「可重新生成」的技术元数据，
    说明是「人工资产」。混在一起的话，每重跑一次生成器就要重写一遍说明。
    """
    if path is None or not path.exists():
        return {"tables": {}, "columns": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {"tables": raw.get("tables", {}), "columns": raw.get("columns", {})}


def _lookup(mapping: dict[str, str], t_schema: str, table: str, column: str | None = None) -> str:
    """说明文件取键：先试限定名，再退回裸表名。

    退回是为了让 v1.0.0 时代写的 Chinook 说明文件（键形如 `album.title`）
    一个字都不用改就继续可用；多 schema 说明则用 `Sales.SalesOrderHeader.TotalDue`。
    """
    suffix = f".{column}" if column else ""
    for candidate in (f"{t_schema}.{table}{suffix}", f"{table}{suffix}"):
        if candidate in mapping:
            return mapping[candidate]
    return ""


def _table_key(node: exp.Expression) -> Key:
    """从一个表节点取出 (schema, 表名)。

    实测（sqlglot 27.x，dialect=postgres）：
        CREATE TABLE public.album  -> stmt.this 是 Schema，.this 是 Table，Table.db == "public"
        CREATE TABLE album         -> Table.db == ""（未限定）
        CREATE TABLE sales."Order" -> Table.db == "sales"
    即 schema 位在 `.db`，未限定时空串。catalog（三段名的库名）本项目用不到，忽略。
    """
    if isinstance(node, exp.Schema):
        node = node.this
    if not isinstance(node, exp.Table):
        return (DEFAULT_SCHEMA, "")
    return (node.db or DEFAULT_SCHEMA, node.name)


def _resolve(tables: "OrderedDict[Key, dict]", t_schema: str, table: str) -> Key | None:
    """把 (schema, 表名) 解析到实际存在的键。

    ALTER / INSERT 语句未必带 schema 限定，而 CREATE TABLE 可能带了。
    所以先精确匹配；不中时，若裸表名在全文唯一则退回它（这就是 PostgreSQL
    在 search_path 下找表的实际行为），唯一性不成立则不猜——猜错会静默挂错外键。
    """
    key = (t_schema, table)
    if key in tables:
        return key
    candidates = [k for k in tables if k[1] == table]
    return candidates[0] if len(candidates) == 1 else None


def load_statements(ddl_path: Path) -> list[exp.Expression]:
    """读取建库脚本并切句。psql 元命令（\\c / \\i 等）sqlglot 不认，先剔除。"""
    text = ddl_path.read_text(encoding="utf-8", errors="replace")
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("\\"))
    return [s for s in sqlglot.parse(text, dialect=DIALECT, error_level=ErrorLevel.IGNORE) if s is not None]


def _normalise_type(type_sql: str) -> str:
    """把静态解析出的类型写法归一到与连库路径相同的形态。

    为什么需要：同一列在两条路径下必须给出同一个字符串，否则「两种来源、一个契约、
    一个产物」这句话在 diff 时就不成立。实测到的两处差异（各 3 列）：
      1) 括号内空格：dump 写 NUMERIC(10, 2)（逗号后带空格），
         连库路径经 information_schema 拼回的是 NUMERIC(10,2)；
      2) 别名：dump 写 DECIMAL，而 PostgreSQL 里 DECIMAL 就是 NUMERIC 的别名，
         information_schema.data_type 回报 NUMERIC。
    只做这两项最小归一（去空白 + 别名），不做更宽的类型改写——
    类型改写属于口径变更，会反过来影响两条路径的可比性。
    """
    t = "".join(type_sql.split()).upper()
    if t.startswith("DECIMAL("):
        t = "NUMERIC" + t[len("DECIMAL") :]
    elif t == "DECIMAL":
        t = "NUMERIC"
    return t


def _column_constraint_kinds(col: exp.ColumnDef) -> set[str]:
    """把列级约束归一成字符串集合。

    坑：ColumnConstraint.kind 不是字符串——NOT NULL 是 NotNullColumnConstraint 节点，
    PRIMARY KEY 是 PrimaryKeyColumnConstraint 节点，直接 .upper() 会 AttributeError。
    """
    kinds: set[str] = set()
    for constraint in col.args.get("constraints") or []:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.NotNullColumnConstraint):
            kinds.add("NOT NULL")
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            kinds.add("PRIMARY KEY")
        elif isinstance(kind, exp.Expression):
            kinds.add(kind.sql(dialect=DIALECT).upper())
        elif isinstance(kind, str):
            kinds.add(kind.upper())
    return kinds


def parse_tables(stmts: list[exp.Expression]) -> "OrderedDict[Key, dict]":
    tables: "OrderedDict[Key, dict]" = OrderedDict()

    for stmt in stmts:
        if not (isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE"):
            continue
        if not isinstance(stmt.this, exp.Schema):
            continue
        t_schema, tname = _table_key(stmt.this)
        if not tname:
            continue
        columns: list[dict] = []
        pk: list[str] = []

        for item in stmt.this.expressions:
            if isinstance(item, exp.ColumnDef):
                kinds = _column_constraint_kinds(item)
                kind = item.args.get("kind")
                columns.append(
                    {
                        "name": item.name,
                        "type": _normalise_type(kind.sql(dialect=DIALECT)) if kind else "UNKNOWN",
                        "nullable": "NOT NULL" not in kinds,
                        "is_primary_key": "PRIMARY KEY" in kinds,
                    }
                )
                if "PRIMARY KEY" in kinds:
                    pk.append(item.name)
            elif isinstance(item, exp.PrimaryKey):
                pk = [c.name for c in item.expressions]
            elif isinstance(item, exp.Constraint):
                # 形如：CONSTRAINT album_pkey PRIMARY KEY (album_id)
                for node in item.walk():
                    if isinstance(node, exp.PrimaryKey):
                        for c in node.expressions:
                            if c.name not in pk:
                                pk.append(c.name)

        for col in columns:
            if col["name"] in pk:
                col["nullable"] = False
                col["is_primary_key"] = True

        tables[(t_schema, tname)] = {
            "table": tname,
            "schema": t_schema,
            "primary_key": pk,
            "columns": columns,
            "foreign_keys": [],
        }

    return tables


def parse_foreign_keys(stmts: list[exp.Expression], tables: "OrderedDict[Key, dict]") -> None:
    """解析 ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY。

    坑：sqlglot 把外键包了两层——
    Alter.actions -> [AddConstraint(expressions=[Constraint(expressions=[ForeignKey])])]，
    所以只能 walk 找 ForeignKey，不能只看 AddConstraint.args['expression']。

    另一个坑：REFERENCES 的 this 是 Schema（表 + 列）而不是 Table，
    所以「参考表名」要从 Schema.this 取，列从 Schema.expressions 取。

    v1.1.0 起同时取参考表的 schema：AdventureWorks 的外键是跨 schema 的
    （如 Sales.SalesOrderHeader -> Person.Address），只给表名无法唯一定位。
    """
    for stmt in stmts:
        if not isinstance(stmt, exp.Alter):
            continue
        target = stmt.this
        if not isinstance(target, exp.Table):
            continue
        local_key = _resolve(tables, target.db or DEFAULT_SCHEMA, target.name)
        if local_key is None:
            continue
        for action in stmt.args.get("actions") or []:
            if not isinstance(action, exp.AddConstraint):
                continue
            for node in action.walk():
                if not isinstance(node, exp.ForeignKey):
                    continue
                ref = node.args.get("reference")
                if ref is None:
                    continue
                ref_this = ref.this
                if isinstance(ref_this, exp.Schema):
                    ref_schema, ref_table = _table_key(ref_this)
                    ref_cols = [c.name for c in ref_this.expressions]
                elif isinstance(ref_this, exp.Table):
                    ref_schema, ref_table = _table_key(ref_this)
                    ref_cols = [c.name for c in ref.expressions]
                else:
                    continue
                tables[local_key]["foreign_keys"].append(
                    ForeignKeyMeta(
                        columns=[c.name for c in node.expressions],
                        references=ForeignKeyRef(schema=ref_schema, table=ref_table, columns=ref_cols),
                    )
                )


def collect_row_stats(stmts: list[exp.Expression], tables: "OrderedDict[Key, dict]", max_samples: int) -> None:
    """顺带把 INSERT 也解析了：行数 + 去重样例值。

    为什么值得做：样例值是「要素识别」阶段判别名（北京/帝都）与看出字段口径的第一手材料，
    没有数据库也能拿到，就先给 A。
    """
    rows: dict[Key, list[dict]] = {k: [] for k in tables}

    for stmt in stmts:
        if not isinstance(stmt, exp.Insert):
            continue
        target = stmt.this
        if isinstance(target, exp.Schema):
            t_schema, tname = _table_key(target)
            colnames = [c.name for c in target.expressions]
        elif isinstance(target, exp.Table):
            t_schema, tname = _table_key(target)
            colnames = None
        else:
            continue
        key = _resolve(tables, t_schema, tname)
        if key is None or colnames is None:
            continue
        values = stmt.expression
        if not isinstance(values, exp.Values):
            continue
        for tup in values.expressions:
            vals = []
            for v in tup.expressions:
                if isinstance(v, (exp.Literal, exp.National)):
                    # N'...' 在 sqlglot 里解析为 National 节点，不是 Literal 的子类。
                    # 若让它落到下面的 else 分支取 .sql()，会把 dump 的书写形式
                    # （N'AC/DC'，含 N 与前后引号）当成字段值写进元数据，
                    # 污染后续「要素识别 / Schema Linking」的向量文本。
                    # 值本身只在 .this 里（实测：National.this == 'AC/DC'）。
                    # 普通字符串与日期串（如 '1962/2/18'）是 Literal，走同一条路。
                    vals.append(v.this)
                elif isinstance(v, exp.Null):
                    vals.append(None)
                else:
                    vals.append(v.sql(dialect=DIALECT))
            rows[key].append(dict(zip(colnames, vals)))

    for key, trows in rows.items():
        tables[key]["row_count"] = len(trows)
        for col in tables[key]["columns"]:
            seen, samples = set(), []
            for r in trows:
                v = r.get(col["name"])
                if v is None or v == "":
                    continue
                if v not in seen:
                    seen.add(v)
                    samples.append(v)
                if len(samples) >= max_samples:
                    break
            col["sample_values"] = samples


def build(ddl_path: Path, database: str, max_samples: int, desc_path: Path | None = None) -> SchemaMeta:
    stmts = load_statements(ddl_path)
    if not stmts:
        raise SystemExit(f"没有从 {ddl_path} 解析出任何语句，请检查文件是否完整。")

    tables = parse_tables(stmts)
    if not tables:
        raise SystemExit(f"没有解析出 CREATE TABLE，{ddl_path} 可能不是建库脚本。")

    parse_foreign_keys(stmts, tables)
    collect_row_stats(stmts, tables, max_samples)

    desc = load_descriptions(desc_path)

    table_metas = [
        TableMeta(
            table=t["table"],
            schema=t["schema"],
            description=_lookup(desc["tables"], t["schema"], t["table"]),
            row_count=t.get("row_count"),
            primary_key=t["primary_key"],
            columns=[
                ColumnMeta(**c, description=_lookup(desc["columns"], t["schema"], t["table"], c["name"]))
                for c in t["columns"]
            ],
            foreign_keys=t["foreign_keys"],
        )
        for t in tables.values()
    ]

    return SchemaMeta(
        database=database,
        dialect=DIALECT,
        source=ddl_path.name,
        generated_at=now_utc_iso(),
        generator="gen_schema_meta_from_ddl.py (静态解析，未经数据库校验)",
        tables=table_metas,
    )


def self_check(meta: SchemaMeta) -> list[str]:
    """结构性自检。返回问题列表；空列表才算通过。

    这些检查是确定性的（纯函数式遍历），目的是不让「外键指向不存在的表」
    这类错误被 A 在使用时才发现。
    """
    problems: list[str] = []
    by_qualified = {t.qualified_name(): t for t in meta.tables}

    for t in meta.tables:
        cols = t.column_names()
        if not t.primary_key:
            problems.append(f"{t.qualified_name()}: 未解析到主键")
        for pk_col in t.primary_key:
            if pk_col not in cols:
                problems.append(f"{t.qualified_name()}: 主键列 {pk_col} 不在字段列表里")
        if t.row_count is None:
            problems.append(f"{t.qualified_name()}: 未解析到任何 INSERT，行数未知")
        for fk in t.foreign_keys:
            target = fk.references.qualified_name()
            if target not in by_qualified:
                problems.append(f"{t.qualified_name()}: 外键指向不存在的表 {target}")
            else:
                ref_cols = by_qualified[target].column_names()
                for rc in fk.references.columns:
                    if rc not in ref_cols:
                        problems.append(f"{t.qualified_name()}: 外键指向 {target}.{rc}，该列不存在")
            for lc in fk.columns:
                if lc not in cols:
                    problems.append(f"{t.qualified_name()}: 外键本地列 {lc} 不在字段列表里")

    seen: set[str] = set()
    for t in meta.tables:
        q = t.qualified_name()
        if q in seen:
            problems.append(f"限定名重复：{q}")
        seen.add(q)

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="静态解析建库脚本生成 schema_meta.json")
    ap.add_argument("--ddl", type=Path, default=DEFAULT_DDL, help=f"建库脚本路径（默认 {DEFAULT_DDL}）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出路径（默认 {DEFAULT_OUT}）")
    ap.add_argument("--database", default="chinook", help="写入 JSON 的库名")
    ap.add_argument("--samples", type=int, default=8, help="每列保留的去重样例值个数")
    ap.add_argument(
        "--descriptions",
        type=Path,
        default=DEFAULT_DESC,
        help=f"中文说明文件（默认 {DEFAULT_DESC}）；传 none 表示不要说明",
    )
    args = ap.parse_args()

    if not args.ddl.exists():
        raise SystemExit(f"找不到建库脚本：{args.ddl}\n先跑 infra/scripts/fetch_chinook.sh")

    desc_path = None if str(args.descriptions).lower() == "none" else args.descriptions
    meta = build(args.ddl, args.database, args.samples, desc_path)

    problems = self_check(meta)
    if problems:
        print("自检未通过，未写出文件：")
        for p in problems:
            print("  -", p)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(meta.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    stats = meta.stats()
    total_cols = stats["columns"]
    described_cols = sum(1 for t in meta.tables for c in t.columns if c.description)
    described_tables = sum(1 for t in meta.tables if t.description)

    print(f"来源脚本   : {args.ddl}")
    print(f"说明文件   : {desc_path if desc_path else '(未使用)'}")
    print(f"输出       : {args.out}")
    print(f"契约版本   : {meta.contract_version}")
    print(f"契约校验   : SchemaMeta 通过（含限定名唯一性）")
    print(f"schema / 表 / 字段 / 外键 : {stats['schemas']} / {stats['tables']} / {total_cols} / {stats['foreign_keys']}")
    print(f"中文说明覆盖率   : 表 {described_tables}/{stats['tables']}，字段 {described_cols}/{total_cols}")
    print("-" * 78)
    for t in meta.tables:
        fk = ",".join(f"{'/'.join(f.columns)}->{f.references.qualified_name()}" for f in t.foreign_keys) or "-"
        desc = t.description or "(缺说明)"
        print(
            f"{t.qualified_name():<22} 字段={len(t.columns):<3} 行数={str(t.row_count):<6} "
            f"外键={fk:<40} 说明={desc}"
        )
    print("-" * 78)

    missing = [f"{t.qualified_name()}.{c.name}" for t in meta.tables for c in t.columns if not c.description]
    if missing:
        print(f"以下字段缺中文说明（{len(missing)} 个）：")
        for m in missing:
            print("  -", m)
        print("补到 infra/schema_meta.descriptions.zh.json 的 columns 里，再重跑本脚本即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
