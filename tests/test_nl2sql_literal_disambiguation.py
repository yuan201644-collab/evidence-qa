"""消歧器测试：字面量抽取、真值归属过滤、以及「失败即放弃」的边界。

覆盖的契约（与 `app/nl2sql/disambiguator.py` 的文档一一对应）：
  1. 只把「带连字符且含数字」的 token 当编号，普通英文词不算；
  2. 只保留「字面量在其某一列里真实存在」的候选表；
  3. 一个都没命中时返回 None（**不足以支撑排除**，回落原行为）；
  4. 无字面量 / 无执行器 / 超查询预算 / 执行器报错 → 一律 None；
  5. 字面量不满足字符白名单时被丢弃，绝不拼进 SQL。
"""

from app.nl2sql.disambiguator import (
    DEFAULT_MAX_LOOKUPS,
    LiteralDisambiguator,
    extract_literals,
)
from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta


class _Result:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error


class _FakeExecutor:
    """按 `列名=字面量` 的真值表回答；记录收到的 SQL 以便断言。"""

    def __init__(self, present=(), error=None):
        self.present = set(present)
        self.error = error
        self.sql_seen: list[str] = []

    def execute(self, sql):
        self.sql_seen.append(sql)
        if self.error:
            return _Result(error=self.error)
        for key in self.present:
            table, column, literal = key
            if f'"{table}"' in sql and f'"{column}"' in sql and f"'{literal}'" in sql:
                return _Result(rows=[{"?column?": 1}])
        return _Result(rows=[])


def _catalog():
    def table(name, columns):
        return TableMeta(
            name=name,
            schema="cross_source_demo",
            description="",
            columns=tuple(ColumnMeta(column, "text") for column in columns),
        )

    return SchemaCatalog([
        table("work_orders", ("work_order_id", "equipment_id", "opened_at")),
        table("work_order_events", ("work_order_id", "event_type")),
        table("equipment", ("equipment_id", "site_name")),
        table("contracts", ("contract_id", "status")),
    ])


class _Cand:
    def __init__(self, table, schema="cross_source_demo", score=0.7333):
        self.table = table
        self.schema = schema
        self.score = score
        self.qualified_name = f"{schema}.{table}"


# ── 1. 字面量抽取 ────────────────────────────────────────────────────────

def test_extract_literals_requires_dash_and_digit() -> None:
    assert extract_literals("工单 WO-2026-0001 是否完成？") == ("WO-2026-0001",)
    assert extract_literals("设备 DEV-001 的合同") == ("DEV-001",)
    # 纯英文词、纯数字、无连字符的都**不是**编号。
    assert extract_literals("查询 summary 和 2026 年的数据") == ()


def test_extract_literals_is_deduplicated_in_order() -> None:
    text = "WO-2026-0001 与 WO-2026-0001 以及 DEV-001"
    assert extract_literals(text) == ("WO-2026-0001", "DEV-001")


# ── 2. 真值归属过滤 ─────────────────────────────────────────────────────

def test_literal_keeps_only_tables_where_it_really_exists() -> None:
    executor = _FakeExecutor(present={
        ("work_orders", "work_order_id", "WO-2026-0001"),
        ("work_order_events", "work_order_id", "WO-2026-0001"),
    })
    candidates = [_Cand("work_orders"), _Cand("work_order_events"),
                  _Cand("equipment"), _Cand("contracts")]

    result = LiteralDisambiguator(executor).run(
        "工单 WO-2026-0001 是否在合同修复时限内完成？", candidates, _catalog()
    )

    assert result is not None
    # 四张同分表收窄到两张真正含该编号的表。
    assert result.tables == frozenset({
        "cross_source_demo.work_orders", "cross_source_demo.work_order_events",
    })
    assert result.literals == ("WO-2026-0001",)


def test_unrelated_literal_returns_none_rather_than_excluding_everything() -> None:
    executor = _FakeExecutor(present=set())
    candidates = [_Cand("work_orders"), _Cand("equipment")]

    assert LiteralDisambiguator(executor).run(
        "工单 WO-2026-0001 是否完成？", candidates, _catalog()
    ) is None


# ── 3. 失败即放弃 ───────────────────────────────────────────────────────

def test_no_literal_means_no_judgement() -> None:
    executor = _FakeExecutor()
    assert LiteralDisambiguator(executor).run(
        "一共有多少位客户？", [_Cand("work_orders")], _catalog()
    ) is None
    assert executor.sql_seen == []


def test_missing_executor_means_no_judgement() -> None:
    assert LiteralDisambiguator(None).run(
        "工单 WO-2026-0001 是否完成？", [_Cand("work_orders")], _catalog()
    ) is None


def test_executor_error_means_no_judgement() -> None:
    executor = _FakeExecutor(error="SQL 执行失败：连接超时")
    assert LiteralDisambiguator(executor).run(
        "工单 WO-2026-0001 是否完成？", [_Cand("work_orders")], _catalog()
    ) is None


def test_query_budget_stops_the_scan() -> None:
    executor = _FakeExecutor(present={("work_orders", "work_order_id", "WO-2026-0001")})
    candidates = [_Cand("work_orders"), _Cand("work_order_events"),
                  _Cand("equipment"), _Cand("contracts")]

    # 4 张表列数合计 10，超过预算 → 放弃判断，且**一次查询都不发**。
    result = LiteralDisambiguator(executor, max_lookups=3).run(
        "工单 WO-2026-0001 是否完成？", candidates, _catalog()
    )
    assert result is None
    assert executor.sql_seen == []


def test_default_budget_is_positive() -> None:
    assert DEFAULT_MAX_LOOKUPS > 0


def test_unknown_table_in_catalog_is_skipped() -> None:
    executor = _FakeExecutor(present={("work_orders", "work_order_id", "WO-2026-0001")})
    result = LiteralDisambiguator(executor).run(
        "工单 WO-2026-0001 是否完成？",
        [_Cand("not_in_catalog"), _Cand("work_orders")],
        _catalog(),
    )
    assert result is not None
    assert result.tables == frozenset({"cross_source_demo.work_orders"})


# ── 4. 注入面收口 ───────────────────────────────────────────────────────

def test_literal_is_escaped_and_quoted_in_sql() -> None:
    executor = _FakeExecutor(present={("work_orders", "work_order_id", "WO-2026-0001")})
    LiteralDisambiguator(executor).run(
        "工单 WO-2026-0001 是否完成？", [_Cand("work_orders")], _catalog()
    )
    assert executor.sql_seen, "应当发出至少一次归属查询"
    first = executor.sql_seen[0]
    # 表名列名走标识符引用，字面量走单引号，且都是 LIMIT 1 的只读探针。
    assert '"cross_source_demo"."work_orders"' in first
    assert "'WO-2026-0001'" in first
    assert first.strip().endswith("LIMIT 1")
    assert first.upper().startswith("SELECT 1 FROM")


def test_unsafe_literal_is_dropped_before_reaching_sql() -> None:
    """白名单不通过的字面量不会被拼进 SQL（执行器一次都不该被调用）。"""
    executor = _FakeExecutor()
    disambiguator = LiteralDisambiguator(executor)
    # 直接喂一个含引号的“字面量”，绕过正则抽取，验证最后一道防线。
    assert disambiguator._exists("s", "t", "c", "WO' OR 1=1 --") is False
    assert executor.sql_seen == []
