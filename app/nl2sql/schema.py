"""schema.py - 结构化链路的「库结构目录」（Schema Catalog）。

它解决什么问题（两件事共用一份事实）：

   ① **给模型看**：生成 SQL 前，模型必须先知道"库里有哪些表、哪些字段、字段什么意思"。
      这份材料由 `render_ddl()` 渲染成一段紧凑文本，塞进提示词。
   ② **卡模型用**：生成之后要判断"表名 / 字段名是不是真的存在"（validator.py）。
      判据同样来自本模块。

   两件事用**同一份数据源**，才能保证"给模型看的"和"用来卡它的"是同一套事实。
   若各拉一份（一个手写词表、一个读元数据），迟早出现"提示词里有这张表、校验却不认"的怪事。

数据源：`infra/schema_meta.json` —— C 用 `infra/scripts/export_schema_meta.py`
从真库导出的**生成物**。
    ⚠️ 它是生成物，**不要手改**：重跑生成脚本会静默抹掉改动。
    要加业务别名请走独立文件 `infra/schema_meta.aliases.zh.json`
    （设计见 docs/要素识别与改写_接口_v0.1.md §十）。

本模块只读不写、无副作用，可单独跑测试。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.nl2sql.config import env_str

#: 默认元数据文件：app/nl2sql/schema.py -> app/nl2sql -> app -> 项目根 / infra
DEFAULT_SCHEMA_META_PATH = Path(__file__).resolve().parents[2] / "infra" / "schema_meta.json"


@dataclass(frozen=True)
class ColumnMeta:
    """一个字段。`description` 是中文业务说明（人工维护，不随生成器覆盖）。"""

    name: str
    type: str
    nullable: bool = True
    is_primary_key: bool = False
    description: str = ""
    sample_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForeignKeyMeta:
    """一条外键：本表 columns 指向 ref_table 的 ref_columns。多表关联靠它。"""

    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableMeta:
    """一张表。"""

    name: str
    schema: str = "public"
    description: str = ""
    row_count: int | None = None
    primary_key: tuple[str, ...] = ()
    columns: tuple[ColumnMeta, ...] = ()
    foreign_keys: tuple[ForeignKeyMeta, ...] = ()

    @property
    def column_names(self):
        """本表全部字段名（小写），供校验做集合判断。"""
        return frozenset(c.name.lower() for c in self.columns)

    def column(self, name):
        """按名字取字段（大小写不敏感）；没有则返回 None。"""
        target = name.lower()
        for col in self.columns:
            if col.name.lower() == target:
                return col
        return None


class SchemaCatalog:
    """一个库的结构目录。**只读**。

    构造只接受"从文件读出来的东西"，不做任何 IO 与推断 —— 这样测试里可以
    直接手搓一个小目录来跑校验，不需要真库。
    """

    def __init__(
        self,
        tables,
        *,
        database="",
        dialect="postgres",
        contract_version="",
        source_path=None,
    ):
        self.database = database
        self.dialect = dialect
        self.contract_version = contract_version
        self.source_path = source_path
        table_rows = list(tables)
        grouped: dict[str, list[TableMeta]] = {}
        for table in table_rows:
            grouped.setdefault(table.name.lower(), []).append(table)
        # Keep the historical bare-name key for unique names. Duplicate names
        # remain addressable by schema.table instead of silently overwriting.
        self.tables = {}
        self.ambiguous_table_names: frozenset[str] = frozenset(
            name for name, matches in grouped.items() if len(matches) > 1
        )
        for name, matches in grouped.items():
            if len(matches) == 1:
                self.tables[name] = matches[0]
            else:
                for table in matches:
                    self.tables[f"{table.schema.lower()}.{table.name.lower()}"] = table
        self._all_tables = tuple(table_rows)

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, raw, *, source_path=None):
        """从元数据字典构造。字段缺失一律给安全默认值，**不抛异常**。

        为什么不抛：这份文件是生成物，生成器的字段集可能随版本变化。
        缺一个可选字段就让整条问数链路起不来，代价远大于收益；
        真正要卡的"表/字段名对不对"由 validator 在**每一条 SQL 上**判，那道关更准。
        """
        tables = []
        for item in raw.get("tables", []):
            name = item.get("table")
            if not name:
                continue
            columns = tuple(
                ColumnMeta(
                    name=c["name"],
                    type=str(c.get("type") or ""),
                    nullable=bool(c.get("nullable", True)),
                    is_primary_key=bool(c.get("is_primary_key", False)),
                    description=(c.get("description") or "").strip(),
                    sample_values=tuple(str(v) for v in (c.get("sample_values") or [])),
                )
                for c in item.get("columns", [])
                if c.get("name")
            )
            foreign_keys = []
            for fk in item.get("foreign_keys", []):
                ref = fk.get("references") or {}
                foreign_keys.append(
                    ForeignKeyMeta(
                        columns=tuple(str(x) for x in (fk.get("columns") or [])),
                        ref_table=str(ref.get("table") or ""),
                        ref_columns=tuple(str(x) for x in (ref.get("columns") or [])),
                    )
                )
            tables.append(
                TableMeta(
                    name=str(name),
                    schema=str(item.get("schema") or "public"),
                    description=(item.get("description") or "").strip(),
                    row_count=item.get("row_count"),
                    primary_key=tuple(str(x) for x in (item.get("primary_key") or [])),
                    columns=columns,
                    foreign_keys=tuple(foreign_keys),
                )
            )
        return cls(
            tables,
            database=str(raw.get("database") or ""),
            dialect=str(raw.get("dialect") or "postgres"),
            contract_version=str(raw.get("contract_version") or ""),
            source_path=source_path,
        )

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw, source_path=str(path))

    @classmethod
    def load(cls, path=None):
        """按默认位置加载（可用环境变量 `SCHEMA_META_PATH` 覆盖）。

        不缓存：调用方（编排器装配 / 工具）自己决定生命周期，
        本模块保持无状态，避免"进程里有一份改了磁盘却读不到的旧目录"。
        """
        if path is None:
            path = env_str("SCHEMA_META_PATH") or DEFAULT_SCHEMA_META_PATH
        return cls.from_file(path)

    # ── 查询 ────────────────────────────────────────────────────────────
    def table_names(self):
        """全部表名（排序，便于稳定输出与比对）。"""
        return sorted(self.tables.keys())

    #: 已知库的展示名。键是 `schema_meta.json` 里的 `database` 值（小写）。
    #: 为什么要一张表而不是直接显示原始库名：`chinook` 这种名字对评委没有信息量，
    #: 「Chinook 示例库」才说明它是题面推荐的公开示例库。表里没有的库**原样显示**，
    #: 不猜、不美化 —— 显示一个编造的友好名比显示真名更糟。
    _DATABASE_LABELS = {
        "chinook": "Chinook 示例库",
        "adventureworks": "AdventureWorks 示例库",
    }

    #: 方言的展示名。同样：表里没有的原样显示。
    _DIALECT_LABELS = {
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "sqlite": "SQLite",
        "mysql": "MySQL",
        "dm": "达梦",
        "kingbase": "人大金仓",
    }

    def data_source(self):
        """本目录指向的数据源标识，供界面告诉用户"答案来自哪个库"。

        只取库名与方言 —— **不含主机、端口、账号、连接串**。这些是运维信息，
        不该出现在面向业务用户的界面上（口径见 `app/contracts.py::DataSource`）。

        库里没有 `database` 字段时返回 None，让调用方决定怎么显示；
        绝不回落成"未知库"之类的假值 —— 那会让界面显示一个并不存在的来源。
        """
        if not self.database:
            return None
        database = self.database.strip()
        if not database:
            return None
        dialect = (self.dialect or "postgres").strip()
        name = self._DATABASE_LABELS.get(database.lower(), database)
        dialect_label = self._DIALECT_LABELS.get(dialect.lower(), dialect)
        return {
            "kind": "database",
            "database": database,
            "dialect": dialect,
            "label": f"{name}（{dialect_label}）",
        }

    def has_table(self, name):
        return bool(name) and self.get_table(name) is not None

    def get_table(self, name):
        key = (name or "").lower()
        direct = self.tables.get(key)
        if direct is not None:
            return direct
        qualified = [table for table in self._all_tables
                     if f"{table.schema}.{table.name}".lower() == key]
        return qualified[0] if len(qualified) == 1 else None

    def has_column(self, table, column):
        meta = self.get_table(table)
        if meta is None:
            return False
        return (column or "").lower() in meta.column_names

    def owner_tables(self, column):
        """哪些表含有这个字段名。用于**无限定名**字段的归属判断。

        返回多张表是常态（`name` 在 artist/album/genre/media_type/track 里都有），
        所以调用方不能"取第一个"，只能问"有没有表认识它"。
        """
        target = (column or "").lower()
        if not target:
            return []
        return [t.name for t in self.tables.values() if target in t.column_names]

    # ── 渲染（给模型看的材料）────────────────────────────────────────────
    def render_ddl(self, *, include_samples=False, max_samples=3, table_names=None):
        """把整库结构渲染成一段紧凑文本，用作提示词里的"库结构"部分。

        **为什么不用标准 DDL（CREATE TABLE）**：DDL 里没有中文业务说明，
        而"供应商"→`customer`、"流派"→`genre` 这类映射恰恰要靠说明才能建立。
        所以我们渲染的是"带注释的字段清单"，比 DDL 长一点，但模型定位表/列准得多。

        `include_samples` 默认关：样例值有助于值级匹配（如国家/职位），
        但会把提示词撑大好几倍。当前先不开，等实测发现值级映射是瓶颈再开。
        """
        requested = {str(name).lower() for name in (table_names or ())}
        selected = [
            (key, table) for key, table in self.tables.items()
            if (not requested or key in requested or table.name.lower() in requested
                or f"{table.schema}.{table.name}".lower() in requested)
        ]
        lines = [
            "【库 " + (self.database or "?")
            + "｜" + (self.dialect or "postgres")
            + "｜" + str(len(selected)) + " 张表】"
        ]
        for key, table in sorted(selected, key=lambda item: item[0]):
            qualified = table.name if table.name.lower() not in self.ambiguous_table_names else key
            lines.append("")
            lines.append("表 " + qualified + "（" + (table.description or "无说明") + "）")
            for col in table.columns:
                lines.append("  - " + _render_column(col, table, include_samples, max_samples))
        return "\n".join(lines)


def _render_column(col, table, include_samples, max_samples):
    """渲染一个字段：名 类型 [约束] [外键指向] — 中文说明 [样例值]。"""
    parts = [col.name, col.type]
    if col.is_primary_key:
        parts.append("主键")
    if not col.nullable:
        parts.append("非空")
    for fk in table.foreign_keys:
        if col.name in fk.columns and fk.ref_table:
            target = fk.ref_columns[0] if fk.ref_columns else "?"
            parts.append("→ " + fk.ref_table + "(" + target + ")")
            break
    text = " ".join(p for p in parts if p)
    if col.description:
        text += " — " + col.description
    if include_samples and col.sample_values:
        shown = list(col.sample_values[:max_samples])
        text += "；样例值：" + "、".join(shown)
    return text


__all__ = [
    "ColumnMeta",
    "ForeignKeyMeta",
    "TableMeta",
    "SchemaCatalog",
    "DEFAULT_SCHEMA_META_PATH",
]
