"""disambiguator.py - 用问题里的编号字面量实查候选表，收窄同分候选。

**它要解决的问题**

`schema_linker` 只吃元数据打分。当多张表各自含有一个「被选中的通用列」时，
`owner_score = column.score * 0.8` 会让这些表拿到**完全相同的分数**，
且它们的 `matched_terms` 相同；`_ambiguity_groups` 因此判定「歧义」并让链路
带着 `need_clarify=True` 拒答。

实测（隔离跨源演示库，问题「工单 WO-2026-0001 是否在合同修复时限内完成？」）：

    cross_source_demo.work_orders         0.7333333333333334
    cross_source_demo.work_order_events   0.7333333333333334
    cross_source_demo.contract_equipment  0.7333333333333334
    cross_source_demo.equipment           0.7333333333333334
    cross_source_demo.contracts           0.24

但问题里已经写明了 `WO-2026-0001`。人看毫无歧义，机器却因为「四张表同分」而拒答。

**为什么必须查库，而不是靠名字猜**

字面量与列名之间**没有**可靠的机械关联：
  - `WO-2026-0001` 与 `work_order_id`：`WO` 是业务缩写，字符层面推不出来；
  - `DEV-001` 与 `equipment_id`：连缩写关系都没有；
  - 按列名首字母缩写会撞车（`contract_id` 与 `customer_id` 都缩成 `ci`）；
  - 只靠中文描述切词也不成立（「工单唯一编号」切出的词块与问题措辞粒度不同）。

所以本模块走**真值确认**：拿字面量去候选表的每一列做一次
`SELECT 1 FROM <table> WHERE <column> = <literal> LIMIT 1`，
**只保留「字面量在其某一列里真实存在」的候选表**。

**边界与纪律**

  - **纯过滤**：不改分数、不改排序、不改 `SchemaLinker`。它只在链路已经拿到
    `ambiguous_groups` 之后才被调用，是「要不要判歧义」的最后一道事实核对。
  - **失败即放弃**：任何异常、超时、连接失败都返回 `None`（= 不做判断），
    让链路回落到原来的歧义行为。**消歧是增强，绝不能把主链路变得更差。**
  - **字面量按不可信输入处理**：本模块的目标执行器（`SqlExecutor`）的
    `execute(sql)` **不接受查询参数**，没有占位符通道可用，因此只能拼字符串。
    故对字面量做**字符白名单**（`[A-Za-z0-9-]`，见 `_SAFE_LITERAL_PATTERN`）
    加 PostgreSQL 字符串字面量转义（双写单引号）双重收口；不满足白名单的
    字面量直接丢弃。表名列名来自元数据，走标识符引用（双写内部双引号）。
  - **查询有上限**：字面量数 × 候选列数 × 候选表数可能膨胀，故设硬上限，
    超限直接放弃（返回 None）而不是把库打爆。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 编号字面量：至少两个字母开头，后接连字符与数字（WO-2026-0001 / DEV-001 / HT-MNT-2026-001）。
# 必须含数字：纯英文单词（如 "summary"）不是编号，不能拿去做归属查询。
_LITERAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,}[A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)(?![A-Za-z0-9])"
)
# 单一长度上限，避免把整段文本当成字面量去打库。
_MAX_LITERAL_LENGTH = 64

# 字面量的字符白名单：编号只可能由字母、数字、连字符组成。
# **这是注入面的收口点**：因为目标执行器没有查询参数通道，字面量只能拼进 SQL，
# 故先用白名单把字符集限死，再叠一层 PostgreSQL 字面量转义。
_SAFE_LITERAL_PATTERN = re.compile(r"[A-Za-z0-9-]+")

#: 一次消歧最多发出的归属查询数。超限即放弃，避免问题很长时把库打爆。
DEFAULT_MAX_LOOKUPS = 24


@dataclass(frozen=True)
class DisambiguationResult:
    """一次消歧的结论。

    Attributes:
        tables: 字面量能在其某列中真实命中的表（qualified key，如 `schema.table`）。
        literals: 参与本次判断的编号字面量。
        lookups: 实际发出的归属查询数。
    """

    tables: frozenset[str]
    literals: tuple[str, ...]
    lookups: int


def extract_literals(question: str) -> tuple[str, ...]:
    """抽出问题里「看起来像编号」的字面量，按出现顺序去重。"""
    found: list[str] = []
    for match in _LITERAL_PATTERN.finditer(question or ""):
        value = match.group(1)
        if len(value) > _MAX_LITERAL_LENGTH:
            continue
        if value not in found:
            found.append(value)
    return tuple(found)


def _quote_ident(name: str) -> str:
    """标识符引用：双写内部双引号，整体加双引号。"""
    return '"' + str(name).replace('"', '""') + '"'


def _quote_literal(value: str) -> str | None:
    """PostgreSQL 字符串字面量。**白名单不通过则返回 None**（调用方须丢弃）。

    目标执行器没有查询参数通道，字面量只能拼接，故此处是安全收口点：
    先要求整个值匹配 `[A-Za-z0-9-]+`，再双写单引号。字符集被限死之后，
    引号注入、反斜杠转义、多语句注入都无处落脚。
    """
    text = str(value or "")
    if not text or not _SAFE_LITERAL_PATTERN.fullmatch(text):
        return None
    return "'" + text.replace("'", "''") + "'"


class LiteralDisambiguator:
    """用编号字面量实查候选表，返回「字面量确实存在」的表集合。

    构造时注入 `executor`（任何有 `execute(sql) -> ExecutionResult` 的对象）。
    **不在本模块 import 具体执行器**，与 `pipeline` 的注入风格保持一致，
    也便于单测传入假执行器。
    """

    def __init__(self, executor, *, max_lookups: int = DEFAULT_MAX_LOOKUPS) -> None:
        self.executor = executor
        self.max_lookups = max(0, int(max_lookups))

    def run(self, question: str, candidates, catalog) -> DisambiguationResult | None:
        """判断字面量归属。

        Args:
            question: 用户问题原文（用原文，不用归一化文本 —— 字面量会被归一化破坏）。
            candidates: 候选表（`LinkCandidate` 序列，取 `schema`/`table`）。
            catalog: 库结构目录，用来取每张候选表的列名。

        Returns:
            `DisambiguationResult`；**无法判断时返回 None**（无字面量、无执行器、
            超出查询上限、或全部查询都失败），调用方应据此回落到原歧义行为。
        """
        if self.executor is None:
            return None
        literals = extract_literals(question)
        if not literals:
            return None

        # 候选表去重，保持稳定顺序（便于测试与日志复现）。
        keys: list[str] = []
        seen: set[str] = set()
        for item in candidates or ():
            schema = str(getattr(item, "schema", "") or "")
            table = str(getattr(item, "table", "") or "")
            if not table:
                continue
            key = f"{schema}.{table}".lower()
            if key in seen:
                continue
            seen.add(key)
            keys.append(f"{schema}.{table}")
        if not keys:
            return None

        columns_by_table: dict[str, tuple[str, ...]] = {}
        planned = 0
        for key in keys:
            schema, _, table = key.rpartition(".")
            columns = self._columns(catalog, schema, table)
            if not columns:
                continue
            columns_by_table[key] = columns
            planned += len(columns) * len(literals)
        if not columns_by_table:
            return None
        # 查询预算：宁可放弃判断，也不把库打爆。
        if planned > self.max_lookups:
            return None

        hit: set[str] = set()
        lookups = 0
        for key, columns in columns_by_table.items():
            if key in hit:
                continue
            schema, _, table = key.rpartition(".")
            for column in columns:
                for literal in literals:
                    lookups += 1
                    if self._exists(schema, table, column, literal):
                        hit.add(key)
                        break
                if key in hit:
                    break
        if not hit:
            # 一个都没命中：可能是字面量本来就不在这几张表里（如问的是合同编号），
            # 也可能是库坏了。**两种都不足以支撑「排除」**，故放弃判断。
            return None
        return DisambiguationResult(
            tables=frozenset(hit), literals=literals, lookups=lookups
        )

    def _columns(self, catalog, schema: str, table: str) -> tuple[str, ...]:
        try:
            for meta in catalog.tables.values():
                if meta.name.lower() != table.lower():
                    continue
                if schema and (meta.schema or "").lower() != schema.lower():
                    continue
                return tuple(column.name for column in meta.columns)
        except Exception:
            return ()
        return ()

    def _exists(self, schema: str, table: str, column: str, literal: str) -> bool:
        """`SELECT 1 ... LIMIT 1` 探一次真值存在性。异常一律视为「不存在」。"""
        quoted = _quote_literal(literal)
        if quoted is None:
            # 白名单不通过：丢弃这个字面量，绝不把它拼进 SQL。
            return False
        qualified = (
            f"{_quote_ident(schema)}.{_quote_ident(table)}"
            if schema
            else _quote_ident(table)
        )
        sql = (
            f"SELECT 1 FROM {qualified} "
            f"WHERE {_quote_ident(column)} = {quoted} LIMIT 1"
        )
        try:
            result = self.executor.execute(sql)
        except Exception:
            return False
        if result is None or getattr(result, "error", None):
            return False
        return bool(getattr(result, "rows", None))


__all__ = [
    "DEFAULT_MAX_LOOKUPS",
    "DisambiguationResult",
    "LiteralDisambiguator",
    "extract_literals",
]