"""口语冗余规则不得吞掉专有名词的回归用例（2026-10-18）。

背景：`_FILLER_RE` 曾把「哈」当作单字语气词裸收，`normalize("哈尔滨工程大学")`
返回「尔滨工程大学」，导致 SQL 的取值过滤整条失效（政采库演示路径实测复现）。
修法是只认句末或标点前的「哈」，本条用例守住该边界。
"""

from __future__ import annotations

import pytest

from app.nl2sql.normalizer import normalize


@pytest.mark.parametrize(
    "question",
    [
        "哈尔滨工程大学",
        "项目名称是哈尔滨工程大学的公告有几条？",
        "查查哈密那边的项目",
    ],
)
def test_filler_keeps_proper_noun_characters(question: str) -> None:
    """专有名词内的「哈」不得被当语气词删除。"""

    result = normalize(question)
    assert "哈尔滨" not in question or "哈尔滨" in result.normalized
    assert "哈密" not in question or "哈密" in result.normalized


def test_filler_still_strips_trailing_particle() -> None:
    """句末语气词仍应被清掉，且留下 filler 改写记录。"""

    result = normalize("库里一共有多少条公告哈")
    assert result.normalized.rstrip("？? ") == "库里一共有多少条公告"
    assert any(change.kind.value == "filler" for change in result.changes)
