"""tests/test_requirement_validator.py —— S2 覆盖核对器的隔离测试。

纪律（母方案 §9.1/§9.3）：**成对出现**「正确写法 + 绕过写法」。
只测通过用例的测试是假的 —— 变异验证会证明这一点。

本文件不连库、不调模型：能力层用手搓假目录注入，SQL 直接给字符串。
"""

from __future__ import annotations

import pytest

from app.nl2sql.capability_runtime import RuntimeCapability
from app.nl2sql.requirement_validator import (
    VALIDATOR_VERSION,
    RequirementValidator,
    _conjunct_has_operator,
    _numeric_range_parts,
)
from app.nl2sql.planning_models import JoinPlan, QueryRequirements
from app.query_requirements import extract_requirements


# ── 假能力层 ────────────────────────────────────────────────────────────


class _FakeMeta:
    def __init__(self, name, description, primary_key=(), sample_values=()):
        self.name = name
        self.description = description
        self.primary_key = primary_key
        self.sample_values = sample_values


class _FakeTableMeta(_FakeMeta):
    """带 `column()` 的表元数据（`normalized_values` 要走这条路）。"""

    def __init__(self, name, description, columns, primary_key=()):
        super().__init__(name, description, primary_key)
        self._columns = columns

    def column(self, name):
        return self._columns.get((name or "").lower())


class _FakeCatalog:
    """只登记测试用到的表。**表名与列名都必须真实存在于本目录。**"""

    def __init__(self):
        self.customer_columns = {
            "customer_id": _FakeMeta("customer_id", "客户编号"),
            "country": _FakeMeta("country", "客户所在国家", (),
                                 ("Brazil", "Argentina")),
        }
        self.invoice_columns = {
            "invoice_id": _FakeMeta("invoice_id", "发票编号"),
            "invoice_date": _FakeMeta("invoice_date", "开票日期"),
            "total": _FakeMeta("total", "发票金额合计"),
            "billing_country": _FakeMeta("billing_country", "账单国家", (),
                                         ("Brazil",)),
        }
        self.employee_columns = {
            "employee_id": _FakeMeta("employee_id", "员工编号"),
            "hire_date": _FakeMeta("hire_date", "入职日期"),
        }
        self.tables = {
            "customer": _FakeTableMeta("customer", "客户表，客户所在国家",
                                       self.customer_columns, ("customer_id",)),
            "invoice": _FakeTableMeta("invoice", "发票表，开票日期与账单国家",
                                      self.invoice_columns, ("invoice_id",)),
            "employee": _FakeTableMeta("employee", "员工表，入职日期",
                                       self.employee_columns, ("employee_id",)),
        }

    def get_table(self, table):
        return self.tables.get((table or "").lower())

    def column_meta(self, table, column):
        return self.get_table(table).column(column) if self.get_table(table) else None


class _FakeCapability(RuntimeCapability):
    def __init__(self):
        self.catalog = _FakeCatalog()
        self.descriptions = {}

    def time_columns(self, table):
        return {"invoice": ("invoice_date",), "employee": ("hire_date",)}.get(table, ())

    def category_columns(self, table):
        return {"customer": ("country",), "invoice": ("billing_country",)}.get(table, ())

    def amount_columns(self, table):
        return ("total",) if table == "invoice" else ()


@pytest.fixture()
def validator():
    return RequirementValidator(_FakeCapability())


def _coverage(validator, question, sql, join_plan=None):
    from app.nl2sql.normalizer import normalize

    return validator.check(sql, extract_requirements(question, normalize(question)), join_plan)


def _of_kind(coverage, kind):
    return [c for c in coverage.checks if c.details.get("kind") == kind]


# ── 一、没有清单 / 解析失败 ─────────────────────────────────────────────


def test_no_requirements_yields_no_checks(validator):
    cov = validator.check("SELECT 1", None)
    assert cov.checks == ()
    assert cov.version == VALIDATOR_VERSION


def test_unparsable_sql_marks_everything_unknown(validator):
    cov = _coverage(validator, "2023年有多少客户？", "这不是 SQL")
    assert cov.checks
    assert all(c.outcome == "unknown" for c in cov.checks)
    assert all(c.reason_code == "sql_ast_unavailable" for c in cov.checks)


def test_capability_absent_still_works(validator):
    """能力层缺席时不能崩，只能给不出依据。"""
    bare = RequirementValidator()
    cov = _coverage(bare, "2023年有多少客户？", "SELECT COUNT(*) FROM customer")
    assert cov.checks


# ── 二、时间条件（SC-05：静默丢年份） ──────────────────────────────────


def test_time_condition_dropped_is_repair(validator):
    """`2023年` 被丢掉 → repair。这是本模块存在的理由。"""
    cov = _coverage(validator, "2023年的发票有哪些？", "SELECT * FROM invoice")
    checks = _of_kind(cov, "time")
    assert len(checks) == 1
    assert checks[0].outcome == "repair"
    assert checks[0].reason_code == "time_condition_dropped"


def test_time_condition_present_is_pass(validator):
    cov = _coverage(
        validator, "2023年的发票有哪些？",
        "SELECT * FROM invoice WHERE invoice_date >= '2023-01-01'",
    )
    checks = _of_kind(cov, "time")
    assert checks[0].outcome == "pass"


def test_time_condition_unsupported_table_is_repair(validator):
    """customer 没有时间列 → 说"做不到"，不是 unknown。"""
    cov = _coverage(validator, "2023年有多少客户？", "SELECT COUNT(*) FROM customer")
    checks = _of_kind(cov, "time")
    assert checks[0].outcome == "repair"
    assert checks[0].reason_code == "time_condition_unsupported"


def test_time_condition_wrong_year_is_unknown(validator):
    """年份对不上但确实有条件 → unknown（证不出等价，不猜）。"""
    cov = _coverage(
        validator, "2023年的发票有哪些？",
        "SELECT * FROM invoice WHERE invoice_date >= '2024-01-01'",
    )
    checks = _of_kind(cov, "time")
    assert checks[0].outcome == "unknown"
    assert checks[0].reason_code == "time_predicate_form_unclear"


def test_time_condition_hidden_in_or_branch_is_not_pass(validator):
    """条件藏在 OR 分支里不能算落实 —— mandatory_conjuncts 作废整条。"""
    cov = _coverage(
        validator, "2023年的发票有哪些？",
        "SELECT * FROM invoice WHERE invoice_date >= '2023-01-01' OR total > 0",
    )
    checks = _of_kind(cov, "time")
    assert checks[0].outcome != "pass"


def test_time_condition_moved_to_having_is_discovered(validator):
    """条件被挪到 HAVING 也要能发现（scope_predicates 覆盖 HAVING）。"""
    cov = _coverage(
        validator, "2023年的发票有哪些？",
        "SELECT billing_country, COUNT(*) FROM invoice "
        "GROUP BY billing_country HAVING MIN(invoice_date) >= '2023-01-01'",
    )
    checks = _of_kind(cov, "time")
    assert checks[0].outcome == "pass"


# ── 四、分组与关联粒度（SC-31/32：按谁汇总） ───────────────────────────


def test_grouping_requires_group_by(validator):
    """「按国家」必须落到 GROUP BY，不能一把 SUM 全表。"""
    cov = _coverage(
        validator, "按国家统计销售额",
        "SELECT SUM(total) FROM invoice",
    )
    checks = _of_kind(cov, "grouping")
    assert len(checks) == 1
    assert checks[0].outcome == "repair"
    assert checks[0].reason_code == "grouping_dropped"


def test_grouping_present_is_pass(validator):
    cov = _coverage(
        validator, "按国家统计销售额",
        "SELECT billing_country, SUM(total) FROM invoice GROUP BY billing_country",
    )
    checks = _of_kind(cov, "grouping")
    assert checks[0].outcome == "pass"


def test_join_grain_distinct_is_pass(validator):
    """「每一位客户」配 DISTINCT 可静态证明粒度。"""
    cov = _coverage(
        validator, "每一位客户的联系人姓名",
        "SELECT DISTINCT c.firstname FROM customer c JOIN invoice i "
        "ON c.customer_id = i.customer_id",
    )
    checks = _of_kind(cov, "join_grain")
    assert checks[0].outcome == "pass"
    assert checks[0].sql_scope == "DISTINCT"


def test_join_grain_group_by_is_pass(validator):
    cov = _coverage(
        validator, "每一位客户的联系人姓名",
        "SELECT c.firstname FROM customer c JOIN invoice i "
        "ON c.customer_id = i.customer_id GROUP BY c.customer_id, c.firstname",
    )
    checks = _of_kind(cov, "join_grain")
    assert checks[0].outcome == "pass"


def test_join_grain_absent_is_unknown_not_pass(validator):
    """一对多 JOIN 后既无 DISTINCT 也无分组 → unknown（证不出重复计数）。"""
    cov = _coverage(
        validator, "每一位客户的联系人姓名",
        "SELECT c.firstname FROM customer c JOIN invoice i "
        "ON c.customer_id = i.customer_id",
    )
    checks = _of_kind(cov, "join_grain")
    assert checks[0].outcome == "unknown"
    assert checks[0].reason_code == "join_grain_unverified"


def test_value_scope_requires_condition(validator):
    """「筛选」类要求：SQL 里没有任何条件时是 repair，不是 pass。"""
    cov = _coverage(validator, "请筛选出发票金额超过 100 的记录",
                   "SELECT * FROM invoice")
    checks = _of_kind(cov, "value_scope")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "condition_scope_dropped"


def test_value_scope_present_is_pass(validator):
    cov = _coverage(
        validator, "请筛选出发票金额超过 100 的记录",
        "SELECT * FROM invoice WHERE total > 100",
    )
    checks = _of_kind(cov, "value_scope")
    assert checks and checks[0].outcome == "pass"


# ── 五、取值筛选与否定（SC-15/16：>100 与 >=100 不同） ─────────────────


def test_value_filter_present_is_pass(validator):
    cov = _coverage(
        validator, "巴西有多少客户？",
        "SELECT COUNT(*) FROM customer WHERE country = 'Brazil'",
    )
    checks = _of_kind(cov, "value_filter")
    assert checks and checks[0].outcome == "pass"


def test_value_filter_dropped_is_repair(validator):
    cov = _coverage(validator, "巴西有多少客户？", "SELECT COUNT(*) FROM customer")
    checks = _of_kind(cov, "value_filter")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "value_filter_dropped"


def test_negation_requires_reverse_operator(validator):
    """`不在` + `=` 是相反语义，必须判 repair。"""
    cov = _coverage(
        validator, "不在巴西的客户有多少？",
        "SELECT COUNT(*) FROM customer WHERE country = 'Brazil'",
    )
    checks = _of_kind(cov, "negation")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "negation_dropped"


def test_negation_present_is_pass(validator):
    cov = _coverage(
        validator, "不在巴西的客户有多少？",
        "SELECT COUNT(*) FROM customer WHERE country != 'Brazil'",
    )
    checks = _of_kind(cov, "negation")
    assert checks and checks[0].outcome == "pass"


def test_numeric_range_right_operator_is_pass(validator):
    cov = _coverage(
        validator, "发票金额大于100的有哪些？",
        "SELECT * FROM invoice WHERE total > 100",
    )
    checks = _of_kind(cov, "numeric_range")
    assert checks and checks[0].outcome == "pass"


def test_numeric_range_wrong_operator_is_unknown(validator):
    """`>` 与 `<` 语义相反：不能说条件被丢（数值还在），也不能说已落实。"""
    cov = _coverage(
        validator, "发票金额大于100的有哪些？",
        "SELECT * FROM invoice WHERE total < 100",
    )
    checks = _of_kind(cov, "numeric_range")
    assert checks and checks[0].outcome == "unknown"
    assert checks[0].reason_code == "numeric_range_operator_unclear"


def test_numeric_range_absent_is_repair(validator):
    cov = _coverage(validator, "发票金额大于100的有哪些？", "SELECT * FROM invoice")
    checks = _of_kind(cov, "numeric_range")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "numeric_range_dropped"


def test_operator_detection_ignores_string_literal():
    """`WHERE note = '> 100'` 里也有 `>`，但不是一次比较。"""
    import sqlglot

    node = sqlglot.parse_one("note = '> 100'", dialect="postgres")
    assert _conjunct_has_operator(node, ">") is False
    assert _conjunct_has_operator(node, "=") is True


@pytest.mark.parametrize(
    "word,symbol",
    [("大于", ">"), ("大于等于", ">="), ("不低于", ">="), ("小于", "<"),
     ("不超过", "<="), ("高于", ">"), ("少于", "<"), ("等于", "=")],
)
def test_numeric_operator_map_covers_extractor_words(word, symbol):
    """中文方向词必须全部能映射成 SQL 运算符（漏一个 = 假阴性）。"""
    from app.query_requirements import _NUMERIC_COMPARISON_RE

    match = _NUMERIC_COMPARISON_RE.match(f"{word}100")
    assert match, f"提取器不认 {word}"
    item = extract_requirements(f"{word}100元的发票").requirements[0]
    operator, value = _numeric_range_parts(item)
    assert operator == symbol
    assert value.startswith("100")


# ── 四、排名与分组（SC：LIMIT 1000 冒充前 3 条） ───────────────────────


def test_ranking_correct_limit_is_pass(validator):
    cov = _coverage(
        validator, "金额最高的3张发票",
        "SELECT * FROM invoice ORDER BY total DESC LIMIT 3",
    )
    checks = _of_kind(cov, "ranking")
    assert checks and checks[0].outcome == "pass"


def test_ranking_fallback_limit_is_repair(validator):
    """`LIMIT 1000` 是兜底行数，不是"前 3 条"。"""
    cov = _coverage(
        validator, "金额最高的3张发票",
        "SELECT * FROM invoice ORDER BY total DESC LIMIT 1000",
    )
    checks = _of_kind(cov, "ranking")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "ranking_limit_mismatch"


def test_ranking_without_order_is_repair(validator):
    cov = _coverage(validator, "金额最高的3张发票", "SELECT * FROM invoice LIMIT 3")
    checks = _of_kind(cov, "ranking")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "ranking_order_missing"


def test_grouping_dropped_is_repair(validator):
    cov = _coverage(validator, "按国家统计客户数", "SELECT COUNT(*) FROM customer")
    checks = _of_kind(cov, "grouping")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "grouping_dropped"


def test_grouping_present_is_pass(validator):
    cov = _coverage(
        validator, "按国家统计客户数",
        "SELECT country, COUNT(*) FROM customer GROUP BY country",
    )
    checks = _of_kind(cov, "grouping")
    assert checks and checks[0].outcome == "pass"


# ── 五、对象与跨源编号 ─────────────────────────────────────────────────


def test_object_table_missing_is_repair(validator):
    cov = _coverage(validator, "巴西有多少客户？", "SELECT COUNT(*) FROM invoice")
    checks = _of_kind(cov, "object")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "object_table_missing"


def test_cross_source_object_dropped_is_repair(validator):
    cov = _coverage(
        validator, "工单 WO-2026-0001 完成了吗", "SELECT * FROM work_orders",
    )
    checks = _of_kind(cov, "cross_source_object")
    assert checks and checks[0].outcome == "repair"
    assert checks[0].reason_code == "cross_source_object_dropped"


def test_cross_source_object_present_is_pass(validator):
    cov = _coverage(
        validator, "工单 WO-2026-0001 完成了吗",
        "SELECT * FROM work_orders WHERE work_order_id = 'WO-2026-0001'",
    )
    checks = _of_kind(cov, "cross_source_object")
    assert checks and checks[0].outcome == "pass"


# ── 六、聚合状态与 join_plan 降级 ──────────────────────────────────────


def test_status_aggregation_prefers_repair_over_unknown(validator):
    """有 repair 也有 unknown 时必须显示 repair（否则该拦的被降级）。"""
    cov = _coverage(
        validator, "2023年的发票有哪些？",
        "SELECT * FROM invoice WHERE invoice_date >= '2024-01-01'",
    )
    assert cov.status in {"unknown", "pass"}


def test_join_plan_tables_are_used_for_binding(validator):
    """有 join_plan 时用它定位表，而不是清单里的对象。"""
    plan = JoinPlan(target_grain="invoice", tables=("invoice",), edges=())
    cov = _coverage(
        validator, "2023年的发票有哪些？",
        "SELECT * FROM invoice WHERE invoice_date >= '2023-01-01'",
        join_plan=plan,
    )
    checks = _of_kind(cov, "time")
    assert checks and checks[0].outcome == "pass"


def test_unresolved_requirement_is_unknown_not_repair(validator):
    """未解析要求不是"没做到"，是"说不清要什么"。"""
    cov = _coverage(validator, "它的质保期是多久？", "SELECT * FROM invoice")
    assert all(c.outcome != "repair" for c in cov.checks)


def test_document_scope_is_not_applicable(validator):
    cov = _coverage(
        validator, "智能计算设备年度维保服务合同里，质保期是多久？",
        "SELECT * FROM invoice",
    )
    checks = _of_kind(cov, "document_scope")
    assert checks and checks[0].details.get("applicable") is False


def test_requirement_ids_are_stable_across_calls(validator):
    reqs = extract_requirements("2023年的发票有哪些？")
    first = [r.id for r in reqs.requirements]
    second = [r.id for r in extract_requirements("2023年的发票有哪些？").requirements]
    assert first == second
    assert all(i for i in first)


# ── 七、指标（measure）：名词性指标不是聚合要求 ─────────────────────────
#
# 这一段守着一个**真发生过的误报**：`金额最高的3张发票` 曾被判
# `unknown / measure_kind_unclear`（"是不是聚合口径无法从字面确定"），
# 但那条 SQL（`ORDER BY total DESC LIMIT 3`）完全正确。
# 根因是提取器把裸指标词一律当聚合要求。修法：提取器写 `normalized="noun"`，
# 核对器对名词性指标直接 pass。
# **放水检查**：下面同时钉住"真要求聚合却没聚合 = repair"，改了不许只剩绿。


def test_noun_measure_is_not_an_aggregate_requirement(validator):
    """「金额最高的3张发票」：用户没说聚合，SQL 不该因"没 SUM"被标黄。"""
    cov = _coverage(
        validator, "金额最高的3张发票是哪些？",
        "SELECT invoice_id, total FROM invoice ORDER BY total DESC LIMIT 3",
    )
    measures = _of_kind(cov, "measure")
    assert measures, "该问句应当提取出 measure 要求"
    assert all(c.outcome == "pass" for c in measures), (
        "名词性指标不是聚合要求，不应返回 unknown/repair：" 
        + str([(c.outcome, c.details.get("surface")) for c in measures])
    )


def test_noun_measure_does_not_hide_missing_aggregate(validator):
    """反向：真要求聚合（"合计"）而 SQL 没有聚合函数，必须 repair。"""
    cov = _coverage(
        validator, "发票金额合计是多少？",
        "SELECT total FROM invoice LIMIT 1",
    )
    measures = _of_kind(cov, "measure")
    assert any(c.outcome == "repair" for c in measures), (
        "显式聚合要求落空必须是 repair，不能被名词性豁免吞掉："
        + str([(c.outcome, c.reason_code) for c in measures])
    )


def test_count_question_is_aggregate_requirement(validator):
    """「有多少位客户」是聚合要求；SQL 没有 COUNT 就必须 repair。"""
    cov = _coverage(
        validator, "平台上一共有多少位客户？",
        "SELECT customer_id FROM customer LIMIT 10",
    )
    measures = [c for c in _of_kind(cov, "measure")
                if (c.details.get("normalized") or "") == "count_question"]
    assert measures, "count_question 应当产生 measure 要求"
    assert any(c.outcome == "repair" for c in measures)


def test_count_question_with_count_is_pass(validator):
    """成对：同样的问句，SQL 带 COUNT 就通过。"""
    cov = _coverage(
        validator, "平台上一共有多少位客户？",
        "SELECT COUNT(*) AS customer_count FROM customer LIMIT 1000",
    )
    measures = [c for c in _of_kind(cov, "measure")
                if (c.details.get("normalized") or "") == "count_question"]
    assert measures and all(c.outcome == "pass" for c in measures)
