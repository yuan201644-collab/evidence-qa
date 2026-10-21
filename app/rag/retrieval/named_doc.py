"""named_doc.py - 题面点名了哪个文档：书名号里的标题 → doc_id

为什么需要这个模块（2026-10-18 实测踩过）：
    题面写「《2026年度增减挂复垦项目验收航飞及技术分析项目（二次）招标公告》
    中载明的预算金额是多少？」——书名号里**已经点名了要查哪一份文档**。
    但检索层只把它当成一串普通查询词，于是召回了同项目的**更正公告**：
    更正公告里正好写着「本项目招标公告中预算金额因笔误录入有误」，
    字面上比招标公告本身还像答案，而真正的预算金额 617500 元在招标公告里。
    答案因此答成 685 万元，判分器判未命中。

    这不是判分口径问题，是检索没听用户的话：**用户点了名，就该只查那份文档。**

设计约束（比功能更重要）：
    **只做精确匹配，不做模糊匹配。** 理由是错配的代价远大于弃配：
    宁可解析不出（退回原来的全库检索，行为与今天一致），
    也不能把「XX项目招标公告」错配到「XX项目更正公告」——那会稳定地
    检索到一份看起来很像、实际错误的文档，比不解析更糟。

    因此只认两类匹配：
        1. 书名号内容与文档标题**完全相同**（去空白、去标点后）
        2. 书名号内容是文档标题的**子串**，且该子串在语料里**唯一**命中一份文档
    两者都失败时返回 None，由调用方维持原行为。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

#: 中文书名号。《…》 是中文里「点名一份具名文档」的确定性标记。
_BOOK_TITLE_RE = re.compile(r"《([^》]{2,})》")

#: 归一化时去掉的字符：空白与常见中英文标点。**保留字母数字与汉字**。
_NORMALISE_STRIP = re.compile(
    r"[\s\u3000，。、；：！？,.;:!?（）()【】\[\]「」\"'`·\-—–_/\"\"]+"
)


def normalise_title(text: str) -> str:
    """把标题压成可比形式：去空白、去标点、小写。"""
    return _NORMALISE_STRIP.sub("", (text or "").lower())


#: 标题行的形态：一行独立文字，长度适中，以「公告/通知/公示/办法/条例/规定…」结尾。
#: 政采公告正文里，标题单独成行且紧跟在「XX 代理机构联系方式」之后、
#: 「项目概况」之前；别的段落都以冒号或编号开头，不会误判。
_TITLE_LINE_RE = re.compile(
    r"^[^\s：:]{6,80}"
    r"(?:招标公告|中标公告|成交公告|更正公告|磋商公告|谈判公告|询价公告|"
    r"废标公告|终止公告|单一来源公示|资格预审公告|公告|通知|公示)$"
)


def extract_title_line(text: str, *, max_lines: int = 200) -> str | None:
    """从一份文档的正文里抽标题行。

    2026-10-18 为什么需要它：政采语料的 `doc_title` 全是 `None`（清洗后
    的纯文本没有标题元数据），于是书名号点名解析在这套语料上完全失效。
    而标题在正文里是**单独一行**存在的，可以抽出来。

    取**最长**的一条候选：正文里「招标公告的公告期限」这类句子也可能
    匹配 `_TITLE_LINE_RE`，但真正的标题通常是最长的那一行。

    参数 max_lines 只扫前若干行：标题一定出现在文档前部，扫全文既慢又
    容易把末尾的附件名当成标题。
    """
    best: str | None = None
    for line in (text or "").splitlines()[:max_lines]:
        candidate = line.strip()
        if not candidate or not _TITLE_LINE_RE.match(candidate):
            continue
        if best is None or len(candidate) > len(best):
            best = candidate
    return best


def extract_named_titles(question: str) -> list[str]:
    """取出题面里所有书名号内容（原始形式，未归一化）。"""
    return _BOOK_TITLE_RE.findall(question or "")


def resolve_named_doc(
    question: str,
    iter_titles: Callable[[], Iterable[tuple[str, str]]],
) -> str | None:
    """题面点名了哪一份文档；解析不出或有歧义时返回 None。

    参数:
        question     用户原问题
        iter_titles  迭代器，产出 (doc_id, title) 二元组。调用方负责从
                     文档元数据里取，本模块不碰数据库（保持可单测）。

    返回:
        唯一命中的 doc_id；没有书名号、没命中、或命中多份（歧义）时返回 None。

    为什么歧义也要返回 None：多份文档共享同一个标题片段时（比如几十份
    「更正公告」），说明用户点的名不足以定位到唯一文档，此时强行挑一份
    就是**猜**。猜错比不管更危险。
    """
    titles = extract_named_titles(question)
    if not titles:
        return None

    # 语料标题只取一次，避免多书名号时重复遍历
    corpus = [(doc_id, normalise_title(title)) for doc_id, title in iter_titles()]
    corpus = [(doc_id, title) for doc_id, title in corpus if title]

    for raw in titles:
        needle = normalise_title(raw)
        if not needle:
            continue

        # 第 1 类：完全相同。优先，因为无歧义风险。
        exact = [doc_id for doc_id, title in corpus if title == needle]
        if len(exact) == 1:
            return exact[0]

        # 第 2 类：书名号内容是标题的子串，但必须**唯一**命中。
        # 注意方向：是「标题包含书名号内容」，不是反过来 ——
        # 用户写的通常是标题的一部分（省略了年份前缀之类），
        # 而不是比标题更长。
        contained = [doc_id for doc_id, title in corpus if needle in title]
        if len(contained) == 1:
            return contained[0]

    return None
