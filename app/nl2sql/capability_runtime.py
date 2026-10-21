"""capability_runtime.py —— 运行时能力判定：「这张表/这个字段能不能支持这类条件」。

**它解决什么问题**：用户说了限定条件，系统却做不到。典型是
`2023年有多少客户？` —— `customer` 表根本没有注册时间列，系统本该说
「客户表没有注册时间依据」，现实却是**静默丢掉年份、返回全量客户数**。

要判「做不到」，必须先回答「这张表有没有这类字段」。母方案 §5.2 设想用
`infra/query_capabilities.v0.1.json` 静态声明，本模块改为**运行时从既有
元数据判定**，四条理由（完整论证见
`docs/architecture/S2-SQL条件核对完整实施计划_v0.1_20261015.md` §3.2）：

1. **静态表恰好丢掉唯一判据**。`customer.country` 与 `invoice.billing_country`
   的列说明分别是「客户所在国家」和「账单国家。按国家统计销售额时用本字段
   （**而非客户所在国家**）」—— 母方案 §5.4 第 3 类要求拦截「账单国家替代
   客户国家」，而判据就藏在后一句话里。静态目录若按 `kind: category` 标注
   这两列，二者完全等价，判不出差异。
2. **schema_meta.json 本身是生成物**（1.2MB 的 AdventureWorks 那套），再手写
   第三份声明只会与真实元数据漂移。
3. **本项目实跑过三个 schema**（Chinook / AdventureWorks / 跨源演示库），
   静态表要维护三套。
4. **所需数据已全部在位**：表/列/主键/外键在 `SchemaCatalog`，业务语义在
   `schema_meta.*.descriptions.zh.json`。

**硬纪律（母方案 §5.2 末句「声明缺失或不匹配时不能按声明通过」）**：

- 说明文件缺失、某列无说明、说明不足以判定 → 一律 `unknown`，**绝不 `pass`**；
- **绝不从列名猜语义** —— `*_date` 后缀猜时间列被明令禁止，只用说明文本；
- 声明的列必须在 `catalog.tables[table].columns` 里真的存在才算；
- 拿不到取值样例 → 空元组，由调用方判 `unknown`，不猜。

**不做 IO**：`SchemaCatalog` 与说明字典都由调用方注入。要读文件请用
`load_capability()` 工厂，测试里可直接手搓一个目录。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.nl2sql.schema import SchemaCatalog

#: 默认列说明文件：app/nl2sql/capability_runtime.py -> app/nl2sql -> app -> 项目根 / infra
DEFAULT_DESCRIPTIONS_PATH = (
    Path(__file__).resolve().parents[2] / "infra" / "schema_meta.descriptions.zh.json"
)

#: 判「这一列是不是时间列」用的词。**只匹配说明文本，不匹配列名**。
#: 为什么不用列名：`birth_date` 是时间列但表示出生年份，`hire_date` 是入职日期，
#: 「去年入职」与「去年出生」是不同的业务口径 —— 靠列名会把两者混为一谈。
#:
#: ⚠ 只用**复合词**不用裸「注册/创建/更新/修改」：AdventureWorks
#: `SalesOrderHeader.ShipToAddressID` 的说明是「…该地址不同于客户**注册地址**」，
#: 裸「注册」会把它误判成时间列（本测试就是抓到这个才收窄的）。
_TIME_TERMS = (
    "时间",
    "日期",
    "年月",
    "年季度",
    "开票",
    "入职",
    "出生",
    "发布日",
    "发布时间",
    "发布日期",
    "注册时间",
    "注册日期",
    "创建时间",
    "创建日期",
    "更新时间",
    "修改时间",
    "签约时间",
    "签约日期",
    "到期时间",
    "有效期",
    "生效",
    "失效",
    "截止",
    "周期",
    "季度",
    "月份",
)

#: 判「这一列能不能证明历史在职/在册状态」用的词。
#: 关键反例：`employee.hire_date`（入职日期）**不算** —— 只有入职日期不能证明
#: 某员工在某一时点仍在职，他可能已经离职（chinook 没有离职列，所以
# `supports_history("employee")` 恒为 False，这是正确结论而非缺失）。
_HISTORY_TERMS = (
    "离职",
    "在职",
    "在册",
    "有效期",
    "生效期",
    "失效",
    "快照",
    "历史版本",
    "停用",
    "启用",
)

#: 判「这一列是不是分类/维度列」用的词。
#: ⚠ 刻意不收「邮编/地址」：`postal_code` 的说明是「邮政编码」、`address` 是
#: 「地址（街道门牌）」—— 它们确实描述地点，但把邮编算成可分组维度会让
#: 「按国家分组」的检查也接受邮编分组。**宁可漏一个维度，不可把邮编当国家。**
#: ⚠ 「性别」是 2026-10-16 补的：AdventureWorks `Employee.Gender` 的说明原先为空，
#: 补说明后若词表也认不出，取值匹配（`app/nl2sql/value_matcher.py`）就永远轮不到它，
#: 「男的有多少？」这类问题会一直答不出。性别是标准的低基数列，收进来不会
#: 把邮编一类的伪维度带进来。
_CATEGORY_TERMS = (
    "国家",
    "城市",
    "州/省",
    "省",
    "州",
    "类型",
    "类别",
    "分类",
    "流派",
    "职位",
    "岗位",
    "状态",
    "名称",
    "性别",
    # 2026-10-18 增补：枚举型业务维度列。
    #
    # 起因是政采题 grob-16「预算级次没法判断的项目有多少个？」被澄清器
    # 当成「没说数什么」追问。判据本身就写着「可作为分组/筛选维度的列」，
    # 而 `projects.budget_level` 的列说明是「预算级次：central 中央预算、
    # local 地方预算、unknown 未知」，正是典型的可分组枚举维度。
    # 漏的是判据的词表 —— 原先只列了 chinook 那套（流派/职位/状态…），
    # 换成政采库后「级次/方式/级别/档位」这些同性质的词一个都没进表。
    "级次",
    "等级",
    "级别",
    "档位",
    "方式",
)

#: 判「这一列是不是金额列」用的词。
#: ⚠ 不收「合计」裸词：`invoice.total` 的说明「发票金额合计」靠「金额」已能
#: 命中；若收「合计」裸词，任何含「小计/总计」的句子都会误伤。
#: ⚠ 也不收「价格/售价」裸词之外的「标价」：AdventureWorks `Product.ListPrice`
#: 的说明是「产品当前目录**标价**」，`SalesOrderDetail.LineTotal` 的说明是
#: 「行级 LineTotal **数值**」—— 后者不含任何金额词，所以能力层判不出它是
#: 金额列。这是**刻意的保守**：宁可漏一个金额列让调用方判 `unknown`，
#: 不可把「标价/数值」一律当金额。真正的口径判据来自表级说明里的
#: 「统计销售额时应使用 LineTotal 列」（见 `table_meaning`）。
_AMOUNT_TERMS = (
    "金额",
    "单价",
    "价格",
    "售价",
    "标价",
    "营收",
    "销售额",
    "费用",
)

#: 判「这一列是不是数量列」用的词。
_QUANTITY_TERMS = (
    "数量",
    "个数",
    "条数",
    "人数",
    "次数",
    "张数",
    "计数",
)


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lowered = (text or "").casefold()
    return any(term.casefold() in lowered for term in terms)


def _description_path_from_catalog(catalog: SchemaCatalog) -> Path | None:
    """按 catalog 的来源位置推断兄弟说明文件。

    优先级与 `app/nl2sql/linker.py::_sibling_descriptions_path` 一致：
    显式环境变量 > catalog 的来源文件同目录 > 项目默认位置。
    """
    configured = os.environ.get("SCHEMA_META_DESCRIPTIONS_PATH", "").strip()
    if configured:
        return Path(configured)
    source_path = getattr(catalog, "source_path", None)
    if source_path:
        schema_path = Path(source_path)
        candidate = schema_path.with_name(schema_path.stem + ".descriptions.zh.json")
        if candidate.is_file():
            return candidate
    configured_schema = os.environ.get("SCHEMA_META_PATH", "").strip()
    if configured_schema:
        candidate = Path(configured_schema)
        sibling = candidate.with_name(candidate.stem + ".descriptions.zh.json")
        if sibling.is_file():
            return sibling
    if DEFAULT_DESCRIPTIONS_PATH.is_file():
        return DEFAULT_DESCRIPTIONS_PATH
    return None


def load_descriptions(path: Path | str | None) -> dict[str, str]:
    """读列说明文件，归一成 `表名.字段名 -> 说明` 的扁平字典。

    兼容两种历史形状：`columns` 段既可能是 `{"customer.country": "..."}` 的
    dict，也可能是 `[{"column": "customer.country", "description": "..."}]`
    的 list（`infra/scripts/export_schema_meta.py::load_descriptions` 两种都产）。

    读不到/解析失败一律返回空字典 —— **空字典让调用方判 `unknown`**，
    不抛异常也不编造说明。
    """
    if path is None:
        return {}
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    descriptions: dict[str, str] = {}
    tables = raw.get("tables")
    if isinstance(tables, dict):
        for key, value in tables.items():
            if isinstance(value, str) and value.strip():
                descriptions[str(key).lower()] = value.strip()
    elif isinstance(tables, list):
        for row in tables:
            if isinstance(row, dict) and str(row.get("table") or "").strip():
                descriptions[str(row["table"]).lower()] = str(row.get("description") or "").strip()
    columns = raw.get("columns")
    if isinstance(columns, dict):
        for key, value in columns.items():
            if isinstance(value, str) and value.strip():
                descriptions[str(key).lower()] = value.strip()
    elif isinstance(columns, list):
        for row in columns:
            if isinstance(row, dict) and str(row.get("column") or "").strip():
                descriptions[str(row["column"]).lower()] = str(row.get("description") or "").strip()
    return descriptions


def _bare_table_name(table: str) -> str:
    """把 `public.invoice` / `db.schema.table` 归一成 catalog 用的裸表名。

    项目里表名有**两种写法并存**：`schema_linker` 与 `join_plan` 给的是
    `public.invoice`（qualified_name），而 catalog 与说明文件用的是
    `invoice`。任何一处漏归一都会让查表静默落空 —— 落空的下场不是报错，
    而是"判不出能力"被当成"没有这个能力"，进而误报条件被丢弃。
    """
    name = (table or "").strip()
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    return name.lower()


class RuntimeCapability:
    """按需回答「这个表/字段支持不支持这类条件」。

    全部方法**只读说明文本与 catalog**，不查库、不调模型、无 IO。
    """

    def __init__(self, catalog: SchemaCatalog, descriptions: Mapping[str, str] | None = None):
        self.catalog = catalog
        self.descriptions = dict(descriptions or {})

    # ── 内部工具 ────────────────────────────────────────────────────────
    def _table(self, table: str):
        """按名取表元数据；**同时接受裸表名与 `schema.table` 两种写法**。

        ⚠ 这个兼容不是可选的：join_plan 给的表名是 `public.invoice` 形式
        （`schema_linker` 的 qualified_name），而 catalog 里登记的是裸
        `invoice`。早期只按原样查，导致 `time_columns("public.invoice")`
        返回空元组，`_check_time` 的第一层判据据此判出
        `time_condition_unsupported`（"相关业务表没有可用的时间依据"）——
        而真实 SQL 里 `invoice_date` 的条件写得完全正确。
        一次字符串不匹配会让整条闸门在**所有带 join_plan 的链路**上
        系统性误报，故在此统一收口，不让调用方各自拼表名。
        """
        meta = self.catalog.get_table(table)
        if meta is not None:
            return meta
        name = (table or "").strip()
        if "." in name:
            # 只取最后一段：schema 可能有多级（`db.schema.table`）。
            return self.catalog.get_table(name.rsplit(".", 1)[-1])
        return None
    def _column_description(self, table: str, column: str) -> str:
        """取 `表名.字段名` 的中文说明；取不到返回空串。

        空串是**有信息量的返回值**：调用方据此判 `unknown`，而不是 `pass`。

        ⚠ 键必须用**裸表名**拼。说明文件里登记的是 `customer.country`
        这种形式，没有 schema 前缀。若调用方传的是 join_plan 给的
        `public.invoice`，拼出来就是 `public.invoice.invoice_date` ——
        永远查不到，于是 `time_columns()` 返回空元组，`_check_time`
        第一层就判 `time_condition_unsupported`。实测该缺陷会让
        「2023年有多少张发票？」这样的**正确答案**被判成条件被丢弃。
        """
        key = f"{_bare_table_name(table)}.{(column or '').lower()}"
        return self.descriptions.get(key, "")

    def _table_description(self, table: str) -> str:
        key = _bare_table_name(table)
        if key in self.descriptions:
            return self.descriptions[key]
        meta = self._table(table)
        return (meta.description if meta is not None else "") or ""

    def _existing_columns(self, table: str) -> tuple[str, ...]:
        """catalog 里真实存在的字段名（小写）。表不存在 → 空元组。"""
        meta = self._table(table)
        if meta is None:
            return ()
        return tuple(sorted(meta.column_names))

    # ── 实体与粒度 ──────────────────────────────────────────────────────
    def primary_key(self, table: str) -> tuple[str, ...]:
        """主键（catalog 里的原始大小写）。空元组 = 无主键或表不存在。"""
        meta = self._table(table)
        return tuple(meta.primary_key) if meta is not None else ()

    def entity_grain(self, table: str) -> str:
        """行粒度。

        - 单列主键 → `one_row_per_<pk>`，是有独立实体的表；
        - 复合主键 → `association`（如 chinook `playlist_track`、
          AdventureWorks `SalesOrderDetail`），**无独立实体**，
          对它做「有多少个」会把关联行数当成实体数；
        - 无主键 → `unknown`（不能声称有稳定粒度）。

        ⚠ `association` 只代表「本表没有独立实体」，不代表它没用：
        `SalesOrderDetail` 正是按订单行统计销售额的唯一正确位置
        （`LineTotal` 的说明原话：「统计销售额时应使用本列」）。
        """
        pk = self.primary_key(table)
        if not pk:
            return "unknown"
        if len(pk) == 1:
            return f"one_row_per_{pk[0]}"
        return "association"

    # ── 时间 ────────────────────────────────────────────────────────────
    def time_columns(self, table: str) -> tuple[str, ...]:
        """该表可用的时间列。判据 = **列说明**含时间语义词。

        无 → 空元组，含义是「这张表没有可用时间依据」。
        `customer` 的全部列说明里无任何时间语义，所以返回 `()`，
        `2023年有多少客户？` 据此判 `repair`（SC-05）。

        实测两套 schema：
        - chinook `invoice` → `('invoice_date',)`；`employee` → `('birth_date','hire_date')`
        - AdventureWorks `SalesOrderHeader` → `('orderdate',)`；
          `BillOfMaterials` → `('startdate','enddate')`（有效期成对出现）

        小写是 `_existing_columns` 统一归一的结果，调用方做集合判断即可。
        """
        return tuple(
            column for column in self._existing_columns(table)
            if _contains_any(self._column_description(table, column), _TIME_TERMS)
        )

    def business_meaning(self, table: str, column: str) -> str:
        """列的业务含义原文，用于给用户解释「为什么不能这么算」。"""
        return self._column_description(table, column)

    def supports_history(self, table: str) -> bool:
        """有无离职/生效期/快照类列，即能不能证明「某一时点在册」。

        chinook 全部 11 表的答案都是 False —— 这是正确结论而非缺失：
        `hire_date` 只说明何时入职，不能说明现在还在职。

        AdventureWorks 同样为 False，但**有另一条正确路径**：
        `EmployeeDepartmentHistory` 的 `StartDate`/`EndDate` 是任职区间，
        所以「2023 年在职员工」应走该表而非 `Employee`。能力层不替调用方
        选表，只如实报告每张表能证明什么。
        """
        return any(
            _contains_any(self._column_description(table, column), _HISTORY_TERMS)
            for column in self._existing_columns(table)
        )

    # ── 分类与取值 ──────────────────────────────────────────────────────
    def category_columns(self, table: str) -> tuple[str, ...]:
        """可作为分组/筛选维度的列（国家、城市、类型、职位、状态等）。

        刻意排除邮编与地址：它们是地点但不是可分组维度。
        """
        return tuple(
            column for column in self._existing_columns(table)
            if _contains_any(self._column_description(table, column), _CATEGORY_TERMS)
        )

    def amount_columns(self, table: str) -> tuple[str, ...]:
        """金额列。"""
        return tuple(
            column for column in self._existing_columns(table)
            if _contains_any(self._column_description(table, column), _AMOUNT_TERMS)
        )

    def quantity_columns(self, table: str) -> tuple[str, ...]:
        """数量列。"""
        return tuple(
            column for column in self._existing_columns(table)
            if _contains_any(self._column_description(table, column), _QUANTITY_TERMS)
        )

    def normalized_values(self, table: str, column: str) -> tuple[str, ...]:
        """规范化取值，取 `ColumnMeta.sample_values`。拿不到 → 空元组。

        调用方据此判 `unknown`，**不得用列名或常识猜取值**（母方案 §5.2）。

        ⚠ 自由文本列（标题、名称、地址）的采样值不是「取值」，而是若干条
        真实记录。它们一旦被当取值，短词就会撞上整条文本 —— 政采库上
        「中标」命中过整条公告标题「…（第二次）中标公告」，进而生成一条
        冒充取值筛选的错条件，还把「本轮有没有提到业务对象」判成没有。

        判据刻意**不用长度**：`customer.country` 的取值是「Argentina」
        「United Kingdom」，比很多标题还长，却是标准的分组维度。
        改用**采样去重后是否少于采样数** —— 枚举维度的取值会重复出现
        （国家、状态、采购方式），自由文本列的三条采样互不相同。
        `sample_values` 来自 `SELECT DISTINCT ... LIMIT 3`，因此只能识别
        「去重后仍不足 3 条」的列；这挡住的是低基数维度被误杀的风险，
        长文本列另有 `value_matcher` 一侧的宽度封顶兜底。
        """
        meta = self._table(table)
        if meta is None:
            return ()
        col = meta.column(column)
        if col is None:
            return ()
        return tuple(str(v).strip() for v in col.sample_values if str(v).strip())

    def value_meaning(self, table: str, column: str) -> str:
        """某一列的取值语义判据原文，取 `ColumnMeta` 上的取值说明。

        本项目元数据暂未登记 `value_notes`，因此当前恒返回空串 —— 这是
        **诚实报告「没有这条依据」**，不是返回空就等于通过。母方案要求
        「无法证明时保留原因」，这里的原因就是「没有取值语义依据」。
        """
        meta = self._table(table)
        if meta is None:
            return ""
        col = meta.column(column)
        if col is None:
            return ""
        return str(getattr(col, "value_notes", "") or "")

    # ── 表级业务含义 ────────────────────────────────────────────────────
    def table_meaning(self, table: str) -> str:
        """表级说明原文。

        chinook `invoice` 的说明写明了「total 为该次结账的金额合计，也是按
        发票头统计销售额/营收时采用的口径」—— 这句话是「按发票头统计销售额」
        的判据。AdventureWorks `SalesOrderDetail` 的说明写明了「统计销售额时
        应使用 LineTotal 列」—— 同一问题在两套 schema 上的答案不同，
        静态能力目录答不了这种差异。
        """
        return self._table_description(table)

    def column_descriptions(self, table: str) -> dict[str, str]:
        """该表全部 `字段名 -> 说明`。说明缺失的字段**不在结果里**。"""
        return {
            column: self._column_description(table, column)
            for column in self._existing_columns(table)
            if self._column_description(table, column)
        }

    def snapshot(self, table: str) -> dict[str, Any]:
        """一张表的能力画像，用于 trace 与验证记录。

        ⚠ 这是**观测接口**，不是判定接口：字段里出现空列表/`unknown` 就是
        空/`unknown`，调用方不得因为「其它字段都有值」就把它当 `pass`。
        """
        return {
            "table": (table or "").lower(),
            "exists": self._table(table) is not None,
            "primary_key": list(self.primary_key(table)),
            "entity_grain": self.entity_grain(table),
            "time_columns": list(self.time_columns(table)),
            "category_columns": list(self.category_columns(table)),
            "amount_columns": list(self.amount_columns(table)),
            "quantity_columns": list(self.quantity_columns(table)),
            "supports_history": self.supports_history(table),
            "table_meaning": self.table_meaning(table),
            "column_descriptions": self.column_descriptions(table),
        }


def load_capability(
    catalog: SchemaCatalog,
    *,
    descriptions_path: Path | str | None = None,
) -> RuntimeCapability:
    """工厂：按默认位置（或显式路径）读说明文件并构造能力判定器。

    说明文件缺失 → 说明字典为空 → 所有能力判定返回 `unknown`。
    这正是母方案要求的「声明缺失时不能按声明通过」。
    """
    if descriptions_path is None:
        path = _description_path_from_catalog(catalog)
    else:
        path = Path(descriptions_path)
    return RuntimeCapability(catalog, load_descriptions(path))


__all__ = [
    "RuntimeCapability",
    "load_capability",
    "load_descriptions",
    "DEFAULT_DESCRIPTIONS_PATH",
]
