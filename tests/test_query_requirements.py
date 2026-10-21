"""S1-1：要求清单提取（`app/query_requirements.py`）的固定测试。

本模块是**纯函数**：无 IO、不加载模型、不碰数据库。测试也保持同样纪律，
只在需要词表时用假词表，避免依赖 Chinook 元数据的具体内容 ——
否则队友改一行中文表说明就会让测试变红，而那不是回归。

覆盖三类容易写错的地方：
  1. **时间坍缩**：`2023年第一季度` 必须整体留成 `2023-Q1`，不能退化成 `2023`。
     方案 §5.3 点名担心"把两个季度折叠成一个年份"，这里守住。
  2. **排名 ≠ 聚合**：`金额最高的3张发票` 是"前 3 条"，不是"求和"。
     同一句里两者都记会让澄清器问错问题。
  3. **澄清触发面**：SC-02「有多少位？」缺对象必须被认出，
     而「平台上一共有多少位客户？」这种完整问句**不能**被误拦。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.query_requirements import (
    REQUIREMENTS_VERSION,
    QueryRequirement,
    QueryRequirements,
    RequirementExtractor,
    extract_requirements,
)


class _FakeVocab:
    """只提供提取器真正用到的两个字段，避免耦合元数据全貌。"""

    tables = ("customer", "invoice", "employee")
    table_desc_alias = {"客户": "customer", "发票": "invoice", "员工": "employee"}


@pytest.fixture()
def extractor() -> RequirementExtractor:
    return RequirementExtractor(vocab_loader=lambda: _FakeVocab())


# ── 时间：不能坍缩 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question,expected",
    [
        ("2023年第一季度相比2023年第二季度的销售额增长率是多少？", ["2023-Q1", "2023-Q2"]),
        ("2023年第1季度和2023年3季度比呢？", ["2023-Q1", "2023-Q3"]),
    ],
)
def test_quarters_survive_side_by_side(extractor, question, expected):
    """两个季度必须各留一条。坍缩成同一个年份 = 方案 §5.3 点名的失败模式。"""
    times = extractor.extract(question).of_kind("time")
    assert [item.normalized for item in times] == expected


@pytest.mark.parametrize(
    "question,expected",
    [
        ("2023年有多少客户？", "2023"),
        ("2023年3月的发票", "2023-03"),
        ("2023-05-06 那天呢", "2023-05-06"),
        ("去年有多少员工？", "去年"),
    ],
)
def test_time_forms_keep_their_granularity(extractor, question, expected):
    times = extractor.extract(question).of_kind("time")
    assert [item.normalized for item in times] == [expected]


# ── 排名与聚合不能互相冒充 ─────────────────────────────────────────────


def test_ranking_is_not_aggregate(extractor):
    """「最高的3张」要的是前 3 行。记成 aggregate 会让澄清器问"按什么分组"。"""
    result = extractor.extract("金额最高的3张发票")
    assert [item.normalized for item in result.of_kind("ranking")] == ["最高3"]
    assert "aggregate" not in {item.normalized for item in result.of_kind("measure")}


def test_explicit_aggregate_is_kept(extractor):
    """反面对照：真正说"一共"的时候，聚合信号要在。"""
    result = extractor.extract("平台上一共有多少位客户？")
    assert "aggregate" in {item.normalized for item in result.of_kind("measure")}


# ── 澄清触发面 ─────────────────────────────────────────────────────────


def test_missing_object_is_detectable(extractor):
    """SC-02「有多少位？」—— 有指标信号、无对象，澄清器据此追问。"""
    result = extractor.extract("有多少位？")
    assert result.has_measure is True
    assert result.has_object is False


def test_complete_question_is_not_flagged(extractor):
    """反面对照：完整问句不能被当成缺对象拦下来。"""
    result = extractor.extract("平台上一共有多少位客户？")
    assert result.has_object is True
    assert [item.normalized for item in result.of_kind("object")] == ["customer"]


def test_deixis_is_unresolved_not_missing(extractor):
    """「它」是有对象但**没落实**。两种情况澄清话术不同，必须能区分。"""
    result = extractor.extract("它的质保期是多久？")
    objects = result.of_kind("object")
    assert len(objects) == 1
    assert objects[0].unresolved is True
    assert objects[0].normalized == "deixis"


# ── 指代被编号锚定时不算未解析 ─────────────────────────────────────
#
# 回归来源：CS14 扩展集的 32 道题全部写成「该工单……工单编号 WO-QF-00X。」
# 对象在同一段里就给出了，而只按词面判定的实现把它们全部判成"缺对象"，
# 在 `tests/test_architecture_comparison_runner.py` 的 CS14 预检里被拦下（误伤 7/32）。
# 追问用户"该指什么"而答案他已经写了，比不追问更糟。


@pytest.mark.parametrize(
    "question",
    [
        "请核验该工单的合同服务恢复时限，计算从受理到服务恢复的时长并给出条款依据。 工单编号 WO-QF-001。",
        "该工单是否刚好达到合同修复上限？请按受理和服务恢复事件核算。 工单编号 WO-QF-002。",
        # 编号跟在句号之后（跨句）——按"同句"限死会漏掉这一例。
        "请确认工单发生时设备与合同的映射是否已经生效，再核验该合同的服务恢复时限。 工单编号 WO-QF-013。",
    ],
)
def test_deixis_anchored_by_work_order_id_is_not_unresolved(extractor, question):
    """编号在同一段里出现时，指代对象已给出，不该判未解析。"""
    result = extractor.extract(question)
    assert [item for item in result.requirements if item.unresolved] == []


def test_deixis_without_anchor_still_unresolved(extractor):
    """没有编号锚定时「该」仍然算未解析——修复不能把追问能力一起关掉。"""
    result = extractor.extract("该合同的质保期是多少？")
    unresolved = [item.surface for item in result.requirements if item.unresolved]
    assert unresolved == ["该"]


def test_far_away_literal_does_not_anchor_deixis(extractor):
    """编号相距过远时不算锚定——否则任意长文里出现个编号就会关掉所有追问。"""
    question = "该合同的质保期是多少？" + "另外，我们还想知道很多其他事情，" * 3 + "工单编号 WO-QF-001。"
    result = extractor.extract(question)
    assert "该" in [item.surface for item in result.requirements if item.unresolved]


# ── 条件类要求在**没有** NormalizeResult 时也要认得出来 ────────────────


def test_works_without_normalize_result(extractor):
    """提取器不能依赖调用方传 `NormalizeResult`。

    假件测试与纯文本调用（含澄清器最需要它的场景）都不传。
    这里断言对象、时间、数值范围三类都从原文直接认出来。
    """
    result = extractor.extract("2023年发票总金额大于100元的有多少张？")
    kinds = {item.kind for item in result.requirements}
    assert {"object", "time", "numeric_range"} <= kinds
    assert [item.normalized for item in result.of_kind("object")] == ["invoice"]
    assert [item.normalized for item in result.of_kind("numeric_range")] == ["大于100元"]


def test_negation_is_its_own_kind(extractor):
    """`不在巴西` 与 `在巴西` 语义相反，不能当普通筛选处理。"""
    result = extractor.extract("不在巴西的客户有多少？")
    assert [item.surface for item in result.of_kind("negation")] == ["不在"]


# ── 词表构件的误伤（回归） ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "其它客户的开票总额是多少？",
        "其中金额最高的是哪张发票？",
        "其实我想看客户数量",
        "我们尤其是想看看发票",
    ],
)
def test_bare_qi_is_not_a_pronoun(extractor, question):
    """裸 `其` 是「其它/其中/其实/尤其」的构件，不是指代。

    收进来会让这些本身完整的话被追问"「其」指的是哪个对象" ——
    已发生过一次回归。`其` 整个不收，宁可漏一次追问也不误伤完整问句。
    """
    objects = extractor.extract(question).of_kind("object")
    assert not any(item.normalized == "deixis" for item in objects), question


def test_ta_inside_qita_is_not_a_pronoun(extractor):
    """「其它」里的 `它` 是构件，不是第三人称指代。

    「其它客户的开票总额是多少？」本身完整；把它判成指代未落实会
    追问"「它」指的是哪个对象或哪份文档"，答非所问。
    """
    objects = extractor.extract("其它客户的开票总额是多少？").of_kind("object")
    assert not any(item.normalized == "deixis" for item in objects)
    assert [item.normalized for item in objects] == ["customer"]


def test_real_pronoun_is_still_caught(extractor):
    """反面对照：真正需要上下文的说法不能被顺手放过。"""
    for question in ("它的质保期是多久？", "这份合同怎么规定的？"):
        objects = extractor.extract(question).of_kind("object")
        assert any(item.unresolved for item in objects), question


# ── 契约形状 ───────────────────────────────────────────────────────────


def test_empty_question_yields_empty_clist():
    result = extract_requirements("   ")
    assert result.requirements == ()
    assert result.has_object is False
    assert result.has_measure is False


def test_requirements_version_is_exposed(extractor):
    """版本号要能被 trace 记下来，否则回溯时不知道用的哪套口径。"""
    assert extractor.extract("有多少位？").version == REQUIREMENTS_VERSION


def test_result_is_immutable(extractor):
    """清单会被写进 trace，冻结可避免下游顺序意外的就地修改。"""
    result = extractor.extract("2023年有多少客户？")
    assert isinstance(result.requirements, tuple)
    with pytest.raises(Exception):
        result.requirements[0].kind = "other"  # type: ignore[misc]


def test_spans_index_into_the_original_question(extractor):
    """span 必须落在**原话**上：澄清器要靠它把原话片段回给用户。"""
    question = "金额最高的3张发票"
    result = extractor.extract(question)
    for item in result.requirements:
        start, end = item.span
        assert question[start:end] == item.surface
