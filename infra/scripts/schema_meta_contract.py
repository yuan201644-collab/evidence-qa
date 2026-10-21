"""schema_meta.json 的数据契约（pydantic v2）。

为什么单独一个文件：
    schema_meta.json 是 C 交给 A 做 Schema Linking 的接口产物，
    改字段 = 改契约 = 必须全员同步（协作规范第一条）。
    把它写成 pydantic 模型，字段名拼错 / 类型写错 / 少给必填项会在生成时就报错，
    而不是等 A 读到 JSON 才发现——这是 pydantic v2 在本项目里的第一个实际用处。

两个生成器都产出同一种结构：
    - gen_schema_meta_from_ddl.py : 静态解析官方建库脚本（无需数据库）
    - export_schema_meta.py       : 连上 PostgreSQL 后导出（权威口径）
两条路径产出必须能被本文件的 SchemaMeta 校验通过。

契约版本历史见 docs/CHANGELOG_schema_meta.md。

v1.1.0（2026-09-18）为什么必须加 schema 字段：
    v1.0.0 只按表名定位，等于隐含假设「所有表都在 public」。
    Chinook 成立（11 张表全在 public），但压力库 AdventureWorks 不成立——
    实测它有 5 个保留大小写的 schema：
        HumanResources(9) / Person(13) / Production(25) / Purchasing(7) / Sales(23) + public(3 视图)
    后果有三个，都不是「少一个字段」这么轻：
      1) 表名会撞。不同 schema 下可以有同名表，键冲突后一张表会把另一张覆盖掉。
      2) NL2SQL 生成的 SQL 天然跑不通。`select * from "Person"."Person"` 缺了 schema
         限定就会报 relation does not exist；这不是 A 的 bug，是接口没给够信息。
      3) 大小写必须原样保留。这些 schema 建库时是带双引号创建的，
         裸写 person.person 会被 PostgreSQL 折叠成小写而找不到对象。
    因此 v1.1.0 在 TableMeta 与 ForeignKeyRef 上各加一个 schema 字段。
    默认值 "public" 保证 v1.0.0 的旧产物（Chinook）读进来仍然合法——升级不破坏存量。
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# pydantic v2 的 BaseModel 上有一个已废弃的 `schema()` 方法，字段名叫 `schema` 会覆盖它，
# pydantic 因此每次导入都发一条 UserWarning。
# 这里选择保留 `schema` 这个字段名：它与 PostgreSQL 术语一致，也是写进 JSON 交给 A 的键；
# 改成 schema_name 只是把麻烦转嫁给下游。
# 因此只精确屏蔽这一条消息，不全局关警告——全局关掉会让后面真正的警告被一起吞掉。
# 已知代价：TableMeta / ForeignKeyRef 上的 pydantic v2 废弃方法 `.schema()` 不可用。
# 本项目与下游统一用 `.schema`（字段）与 `.sql_name()`（拼 SQL），不调用那个方法。
warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema" in ".*" shadows an attribute in parent "BaseModel"',
    category=UserWarning,
)

# 契约版本。任何字段增删改都要 +1，并写进 docs/CHANGELOG_schema_meta.md
CONTRACT_VERSION = "1.1.1"

# PostgreSQL 未加引号的标识符会被折叠成小写并落到 search_path 的首个 schema，
# 本项目统一按 public 记账。显式写出来，别让它是散落各处的字面量。
DEFAULT_SCHEMA = "public"


def normalise_schema(value: str | None) -> str:
    """schema 名的唯一归一入口。

    只做两件事：去掉用户误带的双引号、空值落到 DEFAULT_SCHEMA。
    不做大小写折叠——AdventureWorks 的 schema 是保留大小写的，
    折叠成小写会直接查不到对象（实测 `person.person` 报 relation does not exist）。
    """
    return (value or "").strip().strip('"') or DEFAULT_SCHEMA


def quote_ident_if_needed(name: str) -> str:
    """按 PostgreSQL 规则决定是否给标识符加双引号。

    纯函数，无副作用，便于单测。规则：
      - 全部由小写字母 / 数字 / 下划线组成，且不以数字开头 → 可以裸写；
      - 其余情况（含大写、连字符、保留字）必须加双引号。
    这条规则之所以进契约而不是留给 A 各自实现：AdventureWorks 的 schema 名
    全部是大写开头（Person / Sales / ...），裸写必然报 relation does not exist。
    """
    if name and name[0].isdigit():
        return f'"{name}"'
    ok = all(ch.islower() or ch.isdigit() or ch == "_" for ch in name)
    return name if (ok and name) else f'"{name}"'


class ColumnMeta(BaseModel):
    """一个字段的元信息。A 做 Schema Linking 时，description 与 sample_values 是主召回特征。"""

    name: str
    type: str = Field(description="规范化后的大写类型，如 INT / VARCHAR(160) / TIMESTAMP / NUMERIC(10,2)")
    nullable: bool
    is_primary_key: bool = False
    description: str = Field(default="", description="业务含义（中文）。静态生成时为空，需人工补；连库导出时取 COMMENT")
    sample_values: list[str] = Field(default_factory=list, description="去重后的前若干个取值，供要素识别与别名挖掘用")

    @field_validator("type")
    @classmethod
    def _upper_type(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("sample_values")
    @classmethod
    def _dedup_samples(cls, v: list[str]) -> list[str]:
        seen, out = set(), []
        for item in v:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out


class ForeignKeyRef(BaseModel):
    """外键指向的目标。

    schema 不可省：AdventureWorks 的外键跨 schema（如 Sales.SalesOrderHeader
    指向 Person.Address），只给表名无法唯一定位目标表。
    """

    schema: str = Field(default=DEFAULT_SCHEMA, description="被引用表所在 schema")
    table: str
    columns: list[str]

    @field_validator("schema")
    @classmethod
    def _norm_schema(cls, v: str) -> str:
        return normalise_schema(v)

    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"


class ForeignKeyMeta(BaseModel):
    columns: list[str] = Field(min_length=1)
    references: ForeignKeyRef


class TableMeta(BaseModel):
    table: str
    schema: str = Field(
        default=DEFAULT_SCHEMA,
        description="所在 schema。大小写原样保留（AdventureWorks 的 Person / Sales 等建库时带引号创建）",
    )
    description: str = Field(default="", description="表的中文业务含义，Schema Linking 的表级召回特征")
    row_count: int | None = Field(
        default=None,
        ge=0,
        description="静态解析时为数据行数；连库导出时为 reltuples 估计值。行数不可能为负，故约束 >= 0",
    )
    primary_key: list[str] = Field(default_factory=list)
    columns: list[ColumnMeta] = Field(min_length=1)
    foreign_keys: list[ForeignKeyMeta] = Field(default_factory=list)

    @field_validator("schema")
    @classmethod
    def _norm_schema(cls, v: str) -> str:
        return normalise_schema(v)

    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    def qualified_name(self) -> str:
        """人类可读的限定名，如 Person.Person。用作说明文件与自检的键。"""
        return f"{self.schema}.{self.table}"

    def sql_name(self) -> str:
        """可直接拼进 SQL 的限定名，如 "Person"."Person"。大小写敏感标识符已按需加引号。"""
        return f"{quote_ident_if_needed(self.schema)}.{quote_ident_if_needed(self.table)}"


class SchemaMeta(BaseModel):
    contract_version: str = CONTRACT_VERSION
    database: str = Field(description="库名，如 chinook / adventureworks")
    dialect: Literal["postgres", "dm", "kingbase"] = "postgres"
    source: str = Field(description="来源标识：脚本文件名，或 live:<dsn 的库名>")
    generated_at: str = Field(description="UTC ISO8601，便于 A 判断拿到的是哪一版")
    generator: str
    tables: list[TableMeta] = Field(min_length=1)

    @field_validator("generated_at")
    @classmethod
    def _iso(cls, v: str) -> str:
        datetime.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _no_duplicate_qualified_name(self) -> "SchemaMeta":
        """(schema, table) 必须唯一。

        v1.0.0 只查表名重复，等于默认了单 schema 假设；v1.1.0 起按限定名查。
        这条挡的是「两个 schema 下同名表互相覆盖」——一旦发生，
        下游拿到的是静默缺表而不是报错，最难查。
        """
        seen: set[str] = set()
        dupes: list[str] = []
        for t in self.tables:
            q = t.qualified_name()
            if q in seen:
                dupes.append(q)
            seen.add(q)
        if dupes:
            raise ValueError(f"限定名重复（schema+表名）：{sorted(set(dupes))}")
        return self

    def table_names(self) -> list[str]:
        """裸表名列表。单 schema 场景（Chinook）够用，多 schema 场景请用 qualified_names()。"""
        return [t.table for t in self.tables]

    def qualified_names(self) -> list[str]:
        return [t.qualified_name() for t in self.tables]

    def schemas(self) -> list[str]:
        return sorted({t.schema for t in self.tables})

    def stats(self) -> dict[str, int]:
        return {
            "schemas": len(self.schemas()),
            "tables": len(self.tables),
            "columns": sum(len(t.columns) for t in self.tables),
            "foreign_keys": sum(len(t.foreign_keys) for t in self.tables),
        }


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
