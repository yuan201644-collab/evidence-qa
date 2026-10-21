"""C3 补测：为 M4–M7 四个逃逸的变异各写一个真断言。

这四个变异的共同点：它们破坏的都是"看起来无关紧要"的细节
（收窄守卫、编号稳定性、原文复核）。没测试盯着时它们不会被发现，
但它们分别对应三类真实风险：
  M4/M5 —— 空指令被当成要求，核对器凭空造验收条件
  M6    —— 编号漂移，清单合并/补抽后对不上账
  M7    —— 报出一条用户根本没说的要求
"""
from __future__ import annotations

import pytest

from app.query_requirements import REQUIREMENTS_VERSION, RequirementExtractor


@pytest.fixture
def extractor():
    return RequirementExtractor()


# ── M4 / M5：收窄守卫 ────────────────────────────────────────────────


def test_scope_word_alone_is_not_a_requirement(extractor):
    """「帮我筛选一下」—— 没有别的要求时，单一个"筛选"不构成要求。

    守卫失效（M4）时它会被记成一条 `value_scope`，核对器随后要求 SQL
    里有对应的筛选结构，于是凭空多出一个验收条件。
    """
    result = extractor.extract("帮我筛选一下")
    assert [item.kind for item in result.requirements] == []


def test_grain_word_alone_is_not_a_requirement(extractor):
    """「按一下」—— 同上，粒度词单独出现不是要求。"""
    result = extractor.extract("按一下")
    assert [item.kind for item in result.requirements] == []


def test_scope_word_with_other_requirements_is_recorded(extractor):
    """反面对照：有别的要求时，作用域词要被记下来（守卫不能把该收的也关掉）。"""
    result = extractor.extract("巴西的客户里，按国家筛选一下有多少位？")
    assert "value_scope" in {item.kind for item in result.requirements}


def test_grain_word_with_other_requirements_is_recorded(extractor):
    """反面对照：有对象有指标时，「每个」要被记下来。

    ⚠ 注意它归 `grouping` 而不是 `join_grain` —— 因为「每个客户」
    在中文里首先是分组维度（GROUP BY customer），"逐条结果"的读法
    由「每一位/每一张」这类带量词的形式承载。见 `_GRAIN_TERMS` 注释。
    """
    result = extractor.extract("每个客户的发票总额是多少？")
    kinds = {item.kind for item in result.requirements}
    assert kinds & {"grouping", "join_grain"}
    assert "object" in kinds  # 客户是对象，发票总额是指标


def test_grain_word_alone_is_not_a_requirement(extractor):
    """「帮我筛一下」类空指令 —— 粒度词单独出现不是要求。

    M11（守卫放宽到常记）时，这类空指令会被记成一条 `join_grain`，
    核对器随后要求 SQL 里有对应结构 —— 凭空造出一个验收条件。
    """
    for question in ("帮我筛一下", "按", "分别", "每一位"):
        result = extractor.extract(question)
        assert "join_grain" not in {item.kind for item in result.requirements}, question


def test_one_word_one_kind_grain_vs_grouping(extractor):
    """一词一类：`每个` 只能落到一类，不能 grouping 和 join_grain 各一条。

    同一个词两边都记会让核对器同时去找 GROUP BY 和验去重粒度，判据打架。
    M9（词表混入分组词）与 M10（不跳过已占用跨度）都必须被这条抓住。
    """
    for question in ("每个客户的发票总额是多少？", "每一位客户的发票总额是多少？"):
        kinds = [item.kind for item in extractor.extract(question).requirements]
        assert not ("grouping" in kinds and "join_grain" in kinds), question


# ── M6：编号稳定性 ───────────────────────────────────────────────────


def test_requirement_id_is_stable_across_extractions(extractor):
    """同一句话抽两次，每条要求的 id 必须逐字相同。

    序号编号（M6）在清单补抽/合并时会让同一条要求拿到不同 id，
    于是核对结果对不回原话上。
    """
    question = "2023年发票总金额大于100元的有多少张？"
    first = extractor.extract(question)
    second = extractor.extract(question)
    assert [item.requirement_id for item in first.requirements] == [
        item.requirement_id for item in second.requirements
    ]
    assert all(item.requirement_id for item in first.requirements)


def test_requirement_id_encodes_span_not_index(extractor):
    """id 里带 span，不带序号 —— 序号在插入新要求时会整体漂移。"""
    result = extractor.extract("巴西有多少客户？")
    objects = result.of_kind("object")
    assert objects, "样本必须至少有一个对象要求"
    for item in objects:
        assert item.requirement_id == f"object:{item.span[0]}-{item.span[1]}"


def test_id_property_falls_back_to_span_when_unnumbered(extractor):
    """没编号的旧实例按 span 现算，不崩也不返空。"""
    from app.nl2sql.planning_models import QueryRequirement

    bare = QueryRequirement(kind="object", surface="客户", span=(0, 2))
    assert bare.id == "object:0-2"
    assert bare.requirement_id == ""


# ── M7：原文复核 ──────────────────────────────────────────────────────


def test_no_fabricated_requirement_when_slice_mismatches(extractor):
    """切出来的片段对不上原文时，这条要求必须被丢掉，不能报假的。

    复核被去掉（M7）时，任何位置算错都会变成一条"用户说了但没说过"的要求 ——
    这正是 §5.1 第 2 条要防的事。这里用**带英文的混合句**：正常句子里
    中英边界最容易算错位置，是对这条复核最有效的样本。

    ⚠ 注意这条断言是"结构不变量"而不是"某个具体样本"：即使当前样本
    全都碰巧算对，只要 `_to_requirements` 少了那道复核，这条断言就
    有概率失败。要 100% 抓住它，见下方 `test_fabricated_requirement_is_dropped`
    —— 那条直接喂一个位置必然算错的输入。
    """
    for question in (
        "平台上一共有多少位客户？",
        "Chinook 里 2023 年的发票有多少张？",
        "employee 表里去年入职的有几人？",
    ):
        result = extractor.extract(question)
        for item in result.requirements:
            assert result.original[item.span[0]:item.span[1]] == item.surface, (
                f"{question}: span={item.span} 切出 {item.span!r} 与 surface={item.surface!r} 不一致"
            )


def test_fabricated_requirement_is_dropped(extractor):
    """直接喂一个**位置必然越界**的输入，确认复核会把它丢掉。

    M7 的针对性断言：用一个超出原文长度的 span 造一条要求，
    复核必须在转换时丢掉它（`continue`），而不是照单全收。
    `_to_requirements` 是类方法，经实例调用（与源码里的真实路径一致）。
    """
    from app.query_requirements import _Match

    original = "客户有多少？"
    bogus = _Match(kind="object", surface="不存在的词", start=0, end=99)
    built = extractor._to_requirements(original, [bogus])
    assert built == [], f"位置越界的要求必须被丢掉，实际得到 {[b.surface for b in built]}"


def test_version_bumped_for_new_kinds(extractor):
    """清单版本随"什么叫一条要求"一起升 —— 报告里要能说清用的是哪版口径。"""
    assert REQUIREMENTS_VERSION == "s2.v1"
    # 反面对照：版本不能是空串（否则无法对账）
    assert extractor.extract("客户有多少？").version == REQUIREMENTS_VERSION
