"""SC-33/34：反向核对（多余条件）——SQL 里凭空出现的取值过滤。

## 背景（意见单 `docs/opinions/to-all/20261016-多余条件无检查.md`）

正向核对（要求→SQL）只能证"用户要的做到没有"，证不了反向
"SQL 做的用户要过没有"。真实缺陷：`那去年的呢？` 生成了
`WHERE country = '中国' AND postal_code >= '20250000'…`——
用户没提中国，Chinook 也没有这个取值，恒假条件算出一个
自信的 0，正向检查全部放行（measure/object 都 pass）。

## 口径（负责人 m10899 裁定 P1=(c) 的延伸）

- 凭空字面量过滤 → `unknown`（标黄放行），**不判 repair**：
  静态证不出它一定错，可能是合理的业务默认。
- 刻意只查 `col = '文本常量'`：数字/日期范围由正向 time/numeric
  检查对账；ORDER BY/LIMIT/类型转换不在射程内，避免打坏 T4/T5。
- 依据 = 要求清单（含继承条）里的 `normalized` 与 `surface`
  双向 casefold 匹配——用户说「巴西」、SQL 写 'Brazil' 也认。

## ⚠ 已知边界

依据来源是**本轮提取清单**。编排路径里 value_filter 依赖
`NormalizeResult`（无 norm 时取值匹配不产出，见
`app/query_requirements.py::_augment_from_capability`——它只在
capability 注入时工作）。编排真实调用链一定传 norm，故生产路径
清单完整；单独构造清单的调用方若漏了取值条，反向检查会**漏报**
（不会误报——方向是安全的）。
"""

from __future__ import annotations

from app.nl2sql.normalizer import normalize
from app.nl2sql.requirement_validator import VALIDATOR_VERSION, RequirementValidator
from app.query_requirements import REQUIREMENTS_VERSION, extract_requirements


def _validator() -> RequirementValidator:
    """无能力依据的核对器：正向能力类检查全 unknown，不影响反向判定。"""
    return RequirementValidator()


def _reqs(question: str):
    """按编排同款方式提取（必须传 normalized，否则取值条缺失）。"""
    return extract_requirements(question, normalize(question))


# ── SC-33：凭空取值过滤必须被标出 ────────────────────────────────────


def test_invented_literal_filter_is_flagged_as_unknown():
    """用户没提中国，SQL 里凭空出现 `country = '中国'` → unknown 标黄。"""
    coverage = _validator().check(
        "SELECT COUNT(*) AS customer_count FROM customer "
        "WHERE country = '中国' AND postal_code >= '20250000' "
        "AND postal_code < '20260000' LIMIT 1000",
        _reqs("那去年的呢？"),
    )
    extraneous = [c for c in coverage.checks if c.reason_code == "extraneous_literal_filter"]
    assert len(extraneous) == 1, (
        f"应当恰好标出一条凭空过滤，实际 {len(extraneous)} 条："
        f"{[c.details.get('surface') for c in extraneous]}"
    )
    check = extraneous[0]
    assert check.outcome == "unknown", "凭空条件是 unknown（标黄），不是 repair（拦截）"
    assert check.details.get("surface") == "country=中国"


def test_multiple_invented_filters_are_each_flagged():
    """多个凭空条件每条都要有交代，不能只报第一条。"""
    coverage = _validator().check(
        "SELECT COUNT(*) FROM customer WHERE country = '中国' AND city = '不存在市'",
        _reqs("有多少客户？"),
    )
    extraneous = [c for c in coverage.checks if c.reason_code == "extraneous_literal_filter"]
    assert {c.details.get("surface") for c in extraneous} == {"country=中国", "city=不存在市"}


# ── SC-34：有依据的取值过滤不得误报 ──────────────────────────────────


def test_stated_value_filter_is_not_flagged():
    """用户说了「巴西」，SQL 写 'Brazil'（normalized 对上）→ 不报。"""
    coverage = _validator().check(
        "SELECT COUNT(*) FROM customer WHERE country = 'Brazil'",
        _reqs("巴西有多少客户？"),
    )
    extraneous = [c for c in coverage.checks if c.reason_code == "extraneous_literal_filter"]
    assert extraneous == [], (
        f"用户明确提出的取值不该被报多余：{[c.details.get('surface') for c in extraneous]}"
    )


def test_inherited_session_value_is_not_flagged():
    """继承条（source=session 已并入清单）同样算依据（SC-34 主场景）。"""
    from app.condition_archive import merge_condition_archive
    from app.nl2sql.planning_models import QueryRequirements

    archive = merge_condition_archive(
        None,
        extract_requirements("只看巴西的呢？", normalize("只看巴西的呢？")),
        question="只看巴西的呢？",
        is_followup=False,
    )
    inherited = QueryRequirements(
        version=REQUIREMENTS_VERSION,
        original="",
        normalized_question="",
        requirements=archive.active_requirements,
    )
    assert any(item.kind == "value_filter" for item in inherited.requirements), (
        "前提：档案里应当有继承下来的取值条件"
    )

    coverage = _validator().check(
        "SELECT billing_country, COUNT(*) AS n FROM invoice "
        "WHERE billing_country = 'Brazil' GROUP BY billing_country",
        inherited,
    )
    extraneous = [c for c in coverage.checks if c.reason_code == "extraneous_literal_filter"]
    assert extraneous == [], (
        f"继承来的取值不该被报多余：{[c.details.get('surface') for c in extraneous]}"
    )


# ── 射程边界：刻意不查的形态 ─────────────────────────────────────────


def test_numeric_and_date_literals_are_out_of_scope():
    """数字/日期常量是范围谓词的组成部分，由正向检查对账，这里不报。"""
    coverage = _validator().check(
        "SELECT COUNT(*) FROM invoice WHERE total > 100 "
        "AND invoice_date >= '2023-01-01'",
        _reqs("2023年金额大于100的发票有多少张？"),
    )
    extraneous = [c for c in coverage.checks if c.reason_code == "extraneous_literal_filter"]
    assert extraneous == [], (
        f"数字/日期常量不在反向检查射程内：{[c.details.get('surface') for c in extraneous]}"
    )


def test_empty_requirements_produce_no_extraneous_checks():
    """没有清单时反向检查不产条目——与正向"宁可不判不乱判"同一纪律。"""
    from app.nl2sql.planning_models import QueryRequirements

    coverage = _validator().check(
        "SELECT COUNT(*) FROM customer WHERE country = '中国'",
        QueryRequirements(version=REQUIREMENTS_VERSION, original="", normalized_question=""),
    )
    assert [c for c in coverage.checks if c.reason_code == "extraneous_literal_filter"] == []

