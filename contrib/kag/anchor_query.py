# -*- coding: utf-8 -*-
# Copyright 2026 The VeriMind Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""锚点抽取与命中排序（R6 的纯逻辑部分，不依赖 KAG、不依赖 Neo4j）。

背景（R6）：KAG 平台默认全文索引 ``_default_text_index`` 使用
``standard-no-stop-words`` 分析器，"字母+数字" 连写串（如 ``NYS212001``）
不会成为可检索 token，``search_text`` 按编号查永远 0 命中；而向量检索
对只差一位的编号（``NYS212001`` 与 ``NYS212002``）几乎不可区分。
两条路都断，导致"按编号问事实"这一类问题必然召回失败。

本模块只负责两件事：

1. :func:`extract_anchors` 从问题文本里抽出像编号的字面量；
2. :func:`rank_hits` 把 ``CONTAINS`` 命中的文本块按"命中了几个锚点"排序。

真正的图查询在 :mod:`anchor_chunk_retriever`，这样本模块可以脱机跑断言。
"""

import re
from typing import Dict, Iterable, List, Sequence, Tuple

__all__ = [
    "extract_anchors",
    "rank_hits",
    "looks_like_anchor",
    "MAX_ANCHORS",
]

# 单次查询最多用几个锚点：锚点越多，CONTAINS 的并集越大、排序越容易失真。
MAX_ANCHORS = 8

# 过短的字面量基本都是噪声（单位缩写、单位名），不当作锚点。
MIN_ANCHOR_LEN = 5

# 纯年份（如 2024）不是锚点：它在语料里到处出现，CONTAINS 会命中整库。
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# 日期同理（2024-10-23 / 2024/10/23）：不是业务编号。
_DATE_RE = re.compile(r"^(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}$")

# 按"特异性从高到低"排列。顺序本身不影响最终结果（最后按长度去重），
# 但保持有序便于阅读与后续扩展。
_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    # 中文文号：苏社〔2024〕1号 / 校科字〔2023〕15号
    ("cn_doc_no", re.compile(r"[\u4e00-\u9fa5]{1,8}〔\d{4}〕\d{1,4}号")),
    # 多段编号：HT-MNT-2026-001 / ATT_65041_01 / NYS212001-NYS212012
    ("segmented", re.compile(r"[A-Za-z0-9]{1,12}(?:[-_][A-Za-z0-9]{1,12}){1,4}")),
    # 字母前缀 + 数字：NYS212001 / AHT2024001
    ("alpha_num", re.compile(r"[A-Za-z]{1,10}\d{3,12}[A-Za-z]?")),
    # 数字前缀 + 字母：2016SC01007 / 2024AHT001
    ("num_alpha", re.compile(r"\d{3,12}[A-Za-z]{2,10}\d{0,6}")),
    # 纯数字长串：文件序号、流水号（>=6 位，且排除年份）
    ("digits", re.compile(r"(?<!\d)\d{6,12}(?!\d)")),
)


def _clean(token: str) -> str:
    """去掉两端的分隔符与标点，保留内部结构。"""
    return token.strip(" \t\r\n,.;:，。；：、()（）[]【】<>《》\"'`")


def looks_like_anchor(token: str) -> bool:
    """判断一个候选串是否值得当成锚点。

    规则刻意保守：宁少勿多。多抽一个噪声锚点会把整库都 CONTAINS 进来，
    反而稀释真正命中的那块文本。
    """
    if not token:
        return False
    if len(token) < MIN_ANCHOR_LEN:
        return False
    if not any(ch.isdigit() for ch in token):
        return False
    if not any(ch.isalnum() for ch in token):
        return False
    if _YEAR_RE.match(token):
        return False
    if _DATE_RE.match(token):
        return False
    return True


def extract_anchors(text: str, max_anchors: int = MAX_ANCHORS) -> List[str]:
    """从文本中抽出编号类锚点，按原文出现顺序返回。

    去重规则：忽略大小写；重叠时保留更长的那个（``2016SC01007`` 优先于
    其中间片段 ``SC01007``）。

    Args:
        text: 问题或子查询文本。
        max_anchors: 最多返回几个。

    Returns:
        去重后的锚点列表；没有则返回空列表（调用方应据此跳过图查询）。
    """
    if not text:
        return []

    spans: List[Tuple[int, int, str]] = []
    for _kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            token = _clean(m.group(0))
            if not looks_like_anchor(token):
                continue
            start = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
            spans.append((start, start + len(token), token))

    # 长串优先，避免 "SC01007" 这类子串把 "2016SC01007" 顶掉。
    spans.sort(key=lambda x: (-(x[1] - x[0]), x[0]))

    accepted: List[Tuple[int, int, str]] = []
    occupied: List[Tuple[int, int]] = []
    seen = set()
    for start, end, token in spans:
        # 已被更长的命中覆盖：直接丢弃，连占位都不需要。
        if any(not (end <= s or start >= e) for s, e in occupied):
            continue
        key = token.lower()
        if key in seen:
            # 同一个编号的重复写法（大小写不同）：占住这段区间，
            # 防止其中的数字子串（212001）被当成新锚点。
            occupied.append((start, end))
            continue
        seen.add(key)
        occupied.append((start, end))
        accepted.append((start, end, token))

    accepted.sort(key=lambda x: x[0])
    return [token for _s, _e, token in accepted][:max_anchors]


def rank_hits(
    anchors: Iterable[str],
    rows: Iterable[Dict],
    content_key: str = "content",
) -> List[Tuple[Dict, float, List[str]]]:
    """按"命中了几个锚点"对候选文本块排序。

    排序键（降序）：命中锚点数 → 命中密度（锚点数 / 文本长度）→ 文本更短者优先。
    密度放在第二位，是因为立项名单这类表格块往往长而有料，靠命中数拉不开差距时，
    密度能把它和"顺带提了一句编号"的长文档区分开。

    Args:
        anchors: :func:`extract_anchors` 的输出。
        rows: 每行至少含 ``content`` 字段的字典序列。
        content_key: 正文字段名。

    Returns:
        ``[(row, score, matched_anchors), ...]``，score 落在 ``[0, 1]``。
    """
    anchor_list = [a for a in anchors if a]
    if not anchor_list:
        return []

    lowered = [(a, a.lower()) for a in anchor_list]
    scored: List[Tuple[Dict, float, List[str]]] = []
    for row in rows:
        content = (row.get(content_key) or "").lower()
        if not content:
            continue
        matched = [orig for orig, low in lowered if low in content]
        if not matched:
            continue
        # 命中数占全部锚点的比例，同时用密度做次级信号（最多 0.1 的加成），
        # 最后截到 1.0，保持与其它检索器 score 同量纲。
        coverage = len(matched) / len(anchor_list)
        density = min(len(matched) / max(len(content), 1) * 1000, 0.1)
        scored.append((row, round(min(coverage + density, 1.0), 6), matched))

    scored.sort(key=lambda x: (-x[1], len(x[0].get(content_key) or "")))
    return scored
