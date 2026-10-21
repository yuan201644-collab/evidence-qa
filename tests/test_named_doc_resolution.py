"""题目里用书名号点名文档时的解析：只精确匹配，宁可不解析也不错配。"""

from __future__ import annotations

from app.rag.retrieval.named_doc import (
    extract_named_titles,
    extract_title_line,
    normalise_title,
    resolve_named_doc,
)

DOCS = [
    ("D1", "2026年度增减挂复垦项目验收航飞及技术分析项目（二次）招标公告"),
    ("D2", "2026年度增减挂复垦项目验收航飞及技术分析项目（二次）更正公告"),
    ("D3", "某某项目招标公告"),
    ("D4", "另一份更正公告"),
]


def _titles():
    return iter(DOCS)


def test_exact_title_resolves_to_that_document_not_its_correction():
    """2026-10-18 真实缺陷：点名的招标公告被同项目的更正公告顶掉。

    题面点的是「招标公告」，正确文档里写「预算金额：617500元」；
    更正公告里写「本项目招标公告中预算金额因笔误录入有误」，字面上
    比招标公告本身还像答案。解析必须锁定 D1，不能退化成全库检索。
    """
    question = "《2026年度增减挂复垦项目验收航飞及技术分析项目（二次）招标公告》中载明的预算金额是多少？"
    assert resolve_named_doc(question, _titles) == "D1"


def test_substring_unique_match_resolves():
    assert resolve_named_doc("《某某项目招标公告》的预算？", _titles) == "D3"


def test_unknown_title_returns_none():
    """点名的文档不在语料里 → 不解析，交给调用方维持全库检索。"""
    assert resolve_named_doc("《不存在的文档》的预算？", _titles) is None


def test_no_book_title_returns_none():
    assert resolve_named_doc("预算金额是多少？", _titles) is None


def test_ambiguous_substring_returns_none():
    """书名号内容能命中多份文档时不猜——猜错比不管更危险。"""
    docs = [("A", "某某项目招标文件"), ("B", "某某项目合同")]
    assert resolve_named_doc("《某某项目》?", lambda: iter(docs)) is None


def test_normalise_strips_punctuation_and_space():
    assert normalise_title(" 某某 项目（招标） ") == normalise_title("某某项目招标")


def test_extract_named_titles_keeps_order():
    """书名号内容至少两个字：单字的《甲》不是文档名，不参与点名解析。"""
    assert extract_named_titles("《甲方公告》和《乙方合同》") == ["甲方公告", "乙方合同"]
    assert extract_named_titles("《甲》") == []


#: 政采公告正文的真实开头（截取自 cleaned_v3 里的一份招标公告）。
#: 关键点：`doc_title` 元数据是 None，标题只在正文里以独立一行存在，
#: 所以书名号点名解析必须先从这里把标题抽出来。
ANNOUNCEMENT_BODY = """公告概要：

公告信息：

采购项目名称

2026年度增减挂复垦项目验收航飞及技术分析项目（二次）

品目

服务/专业技术服务/其他专业技术服务

采购单位

滁州市自然资源和规划局

预算金额

￥61.750000万元（人民币）

2026年度增减挂复垦项目验收航飞及技术分析项目（二次）招标公告
项目概况
"""


def test_extract_title_line_from_announcement_body():
    assert extract_title_line(ANNOUNCEMENT_BODY) == (
        "2026年度增减挂复垦项目验收航飞及技术分析项目（二次）招标公告"
    )


def test_extract_title_line_returns_none_for_plain_body():
    assert extract_title_line("预算金额：617500元\n采购单位：某某局") is None


def test_named_resolution_works_when_doc_title_metadata_missing():
    """端到端：元数据没有标题，靠正文抽标题行也能锁定文档。"""
    question = "《2026年度增减挂复垦项目验收航飞及技术分析项目（二次）招标公告》中载明的预算金额是多少？"
    docs = [
        ("D_gkzb", extract_title_line(ANNOUNCEMENT_BODY)),
        ("D_gzgg", "2026年度增减挂复垦项目验收航飞及技术分析项目（二次）更正公告"),
    ]
    assert resolve_named_doc(question, lambda: iter(docs)) == "D_gkzb"
