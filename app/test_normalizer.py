"""test_normalizer.py —— 要素识别与改写（中级任务 1）的自检。

全部离线：不连库、不调大模型、不加载向量模型（C 层用假 embedder）。
断言口径遵循 docs/要素识别与改写_接口_v0.1.md §9 的三层判分：
① 要素级（entities.canonical）② 类型级（degraded_kinds）③ 幂等性（§5 硬要求）。
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from app.nl2sql.config import time_anchor_from_env
from app.nl2sql.normalizer import (
    AliasTable,
    ChangeKind,
    NormalizeResult,
    build_semantic_resolver,
    load_alias_table,
    normalize,
)

ANCHOR = datetime(2024, 6, 15)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def norm(q: str, **kw) -> NormalizeResult:
    kw.setdefault("time_anchor", ANCHOR)
    return normalize(q, **kw)


def canonicals(r: NormalizeResult, kind: str | None = None) -> set[str]:
    return {e.canonical for e in r.entities if kind is None or e.kind == kind}


# ---------------------------------------------------------------------------
# B 层：业务别名（rob-02/03/04）
# ---------------------------------------------------------------------------


def test_alias_buyer_rewritten_to_customer():
    r = norm("这个平台上总共有多少个买家？")
    assert "customer" in r.normalized
    assert "customer" in canonicals(r, "table")
    assert any(e.via == "alias" and e.surface == "买家" for e in r.entities)
    assert ChangeKind.ALIAS in r.degraded_kinds
    # span 必须落在原文上，且 before 与原文切片一致（前端高亮依赖）
    ch = next(c for c in r.changes if c.before == "买家")
    assert r.original[ch.span[0] : ch.span[1]] == "买家"


def test_alias_colleague_maps_to_employee_not_customer():
    """rob-04 的干扰项：同事 ≠ customer（语义相近的人）。"""
    r = norm("公司现在有多少位同事？")
    assert "employee" in r.normalized
    assert "employee" in canonicals(r, "table")
    assert "customer" not in canonicals(r, "table")


def test_alias_genre():
    r = norm("把所有曲风的名字列出来，按字母顺序排。")
    assert "genre" in r.normalized
    assert "genre" in canonicals(r, "table")


def test_alias_invoice_from_danju():
    r = norm("２０２１ 年一共开了多少张单据？")
    assert "invoice" in canonicals(r, "table")


# ---------------------------------------------------------------------------
# 错别字（等长差一字；rob-05 / rob-09）
# ---------------------------------------------------------------------------


def test_typo_artist():
    r = norm("哪位艺术佳给平台带来的收入最多？")
    assert "artist" in r.normalized
    assert ChangeKind.TYPO in r.degraded_kinds


def test_typo_does_not_rewrite_unrelated_phrase_for_already_identified_entity():
    question = "Chinook 中按客户统计总消费金额，列出消费最高的前 5 位客户及金额。"
    r = norm(question)

    assert "消费金额" in r.normalized
    assert "消费最高" in r.normalized
    assert all(change.after != "customer" for change in r.changes)


def test_two_character_fuzzy_aliases_do_not_rewrite_common_chinese_phrases():
    """Short alias collisions must not corrupt ordinary words inside compound questions."""
    validity_question = (
        "核验 WO-2026-7803：设备排期映射和 CT-733 合同主表的日期范围分别怎样？"
        "工单当天是否仍在合同期内？"
    )
    missing_event_question = (
        "WO-2026-7805 的记录里没有 accepted 时间。还能否从服务恢复记录判定修复是否达标？"
        "请说明数据库缺项能支持的结论边界，并引合同原句。"
    )

    validity = norm(validity_question)
    missing_event = norm(missing_event_question)

    assert "合同主表" in validity.normalized
    assert "合同期内" in validity.normalized
    assert "employee" not in validity.normalized
    assert "说明数据库缺项" in missing_event.normalized
    assert "合同原句" in missing_event.normalized
    assert "employee" not in missing_event.normalized
    assert "Hip Hop/Rap" not in missing_event.normalized


def test_b_rag_false_route_phrases_are_preserved_verbatim():
    """B's observed RAG questions must not be rewritten into schema aliases."""
    questions = (
        "智能计算设备年度维保服务合同里，质保期是多久？",
        "问数服务 API 触发限流时返回哪个错误码？默认限流阈值是多少？",
        "服务器巡检记录的扫描件里，本次巡检发现几台异常设备？对应的工单号是多少？",
        "在目录信息丢失的维保合同版本里，一级故障（业务中断）的响应和到场时限分别是多少？",
        "在目录信息丢失的数据中台白皮书版本里，实时链路的端到端时延是多少？基于什么技术实现？",
        "KB-015 规定发生火警时，现场处置顺序是什么？",
        "这份合同里 GPU 加速卡的质保期呢？",
    )

    for question in questions:
        result = norm(question)
        assert result.normalized == question
        assert not any(change.after in {"employee", "invoice"} for change in result.changes)


def test_typo_two_char_unit_price_ambiguity_is_withheld_not_flagged():
    """两字错别字碎片「单介」：**不改写、也不记 unresolved**。

    2026-10-16 改判（app/nl2sql/normalizer.py 的 `_pass_typo`）：把
    `length < 3`（二字片段不采纳）提到 `len(targets) > 1`（报歧义）之前。

    改判前本条断言「记 unresolved，交给澄清环节」。该期望的问题在于：
    两字模糊匹配会与复合词内部普通词撞车（`工单编号`→`单编`→命中「单价/单据/单曲」），
    这类撞车压根不是独立词，却因报歧义分支排在防护之前而产生了拦截信号，
    实测把 CS14 三题全部拦进澄清。换序后二者都静默跳过。

    本条的**改判边界**（此处刻意不放松）：
    歌曲曲目本体（歌曲 → track）仍由别名层正常改写；
    端到端 rob-05 由下游大模型按字段说明兜住（unit_price 的中文说明就是「单价」）。
    即：系统不会替用户去猜「单介」是单价还是单曲，而是把这个碎片整个放过去。
    """
    r = norm("所有歌曲的单介合计是多少？四舍五入保留两位小数。")
    assert "track" in canonicals(r, "table")
    assert "单介" in r.normalized  # 没有被强行改写
    assert not any("单介" in u for u in r.unresolved)  # 也不再报歧义


def test_typo_two_char_fragment_is_not_rewritten_but_also_not_flagged():
    """两字碰撞面「单壮」距「单曲」1、距「单价」1 —— 同样不采纳、不报歧义。

    与上一条同一条判据（`length < 3` 先于 `len(targets) > 1`）。
    构造：surface「单壮」同时距表别名「单曲」与列别名「单价」一字。
    """
    table = AliasTable(
        version="t", updated_at="t",
        tables={"track": ["单曲"], "genre": ["单流派"]},
    )
    table.columns = {"track.unit_price": ["单价"]}
    r = normalize("请统计单壮的数量", aliases=table, time_anchor=ANCHOR)
    assert "单壮" in r.normalized  # 没被改写
    assert not any("单壮" in u for u in r.unresolved)  # 也不再报歧义
    # 收窄后的残余信号只剩「未识别到业务要素」，它由澄清环节按需处理，
    # 与错别字歧义无关 —— 这里断言它**不是**错别字口径的拦截。
    assert not any("疑似错别字" in u for u in r.unresolved)


# ---------------------------------------------------------------------------
# 全角 / 中英混排（rob-06 / rob-07）
# ---------------------------------------------------------------------------


def test_fullwidth_digits_and_alias():
    r = norm("２０２１ 年一共开了多少张单据？")
    assert "２０２１" not in r.normalized and "2021" in r.normalized
    assert "invoice" in canonicals(r, "table")
    assert ChangeKind.FULLWIDTH in r.degraded_kinds
    assert ChangeKind.ALIAS in r.degraded_kinds


def test_fullwidth_punctuation_is_left_alone():
    """全角标点是中文行文的一部分，不转写 —— 只转全角字母/数字。"""
    r = norm("２０２１ 年一共开了多少张单据？")
    assert "？" in r.normalized


def test_mixed_lang_detected_not_rewritten():
    r = norm("List 所有流派的名字，按 name 升序，顺便给出每个流派的 track 数量。")
    assert ChangeKind.MIXED_LANG in r.degraded_kinds
    assert "genre" in canonicals(r, "table")
    assert "track" in canonicals(r, "table")
    assert any(e.via == "exact" and e.surface == "track" for e in r.entities)


# ---------------------------------------------------------------------------
# 口语冗余（rob-08）
# ---------------------------------------------------------------------------


def test_filler_stripped_keep_core():
    r = norm("麻烦帮我查一下哈，就是那个……2022 年一整年嘛，一共开了多少张发票呀？谢谢！")
    assert ChangeKind.FILLER in r.degraded_kinds
    assert "谢谢" not in r.normalized and "麻烦" not in r.normalized and "帮我" not in r.normalized
    # 有效信息必须原样保留
    assert "2022 年" in r.normalized and "发票" in r.normalized and "多少张" in r.normalized


def test_semantic_carrying_colloquial_is_not_filler():
    """rob-11 红线：「最多的那个」承载语义，不许当 filler 删，更不许硬猜改写。"""
    r = norm("帮我看下最多的那个。")
    assert "最多的那个" in r.normalized
    assert canonicals(r) == set() or "unknown" not in canonicals(r)
    assert any(u for u in r.unresolved)  # 没识别到可定位要素 → 如实留痕


def test_ambiguous_question_unchanged():
    r = norm("增长率是多少？")
    assert r.normalized == "增长率是多少？"
    assert r.unresolved, "识别不到要素时必须留痕交给澄清"


# ---------------------------------------------------------------------------
# A 层：元数据派生（识别留痕，不改写）
# ---------------------------------------------------------------------------


def test_desc_keyword_recorded_without_rewrite():
    """「客户」是 customer 的标准中文说法（来自元数据说明）：识别、但不改写。"""
    r = norm("客户总数是多少？")
    assert r.changes == []
    assert "customer" in canonicals(r, "table")
    assert any(e.via == "desc" for e in r.entities)


def test_exact_table_name_recorded():
    r = norm("track 表有多少行？")
    assert any(e.via == "exact" and e.canonical == "track" for e in r.entities)
    assert r.changes == []


# ---------------------------------------------------------------------------
# 值级别名（§7）
# ---------------------------------------------------------------------------


def test_value_alias_usa():
    r = norm("美国客户的总消费是多少？")
    assert "USA" in r.normalized
    assert "USA" in canonicals(r, "value")


# ---------------------------------------------------------------------------
# 时间词（§6；rob-12 ~ rob-18 的真值表，基准 2024-06-15，整期口径）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word, start, end",
    [
        ("去年", "2023-01-01", "2024-01-01"),
        ("前年", "2022-01-01", "2023-01-01"),
        ("今年", "2024-01-01", "2025-01-01"),  # 整期，不是年初至今（37 条那种读法）
        ("上个月", "2024-05-01", "2024-06-01"),
        ("这个月", "2024-06-01", "2024-07-01"),  # 整期
        ("上个季度", "2024-01-01", "2024-04-01"),
        ("本季度", "2024-04-01", "2024-07-01"),  # 整期
    ],
)
def test_time_normalization_ranges(word, start, end):
    r = norm(f"{word}一共开了多少张发票？")
    assert len(r.times) == 1
    t = r.times[0]
    assert t.start == start and t.end == end
    assert t.anchor == "2024-06-15"
    # 标准问句里出现具体年份（label 形如「2023 年」「2024 年 5 月」），下游才能落 WHERE
    assert start[:4] in r.normalized and r.normalized != f"{word}一共开了多少张发票？"


def test_time_anchor_default_is_now():
    """不传 anchor 时取当前时间（生产行为）；这里只验证不抛异常且 anchor 被留存。"""
    r = normalize("去年一共开了多少张发票？")
    assert len(r.times) == 1 and r.times[0].anchor


# ---------------------------------------------------------------------------
# 幂等性（§5 硬要求）：标准问句再跑一遍必须零改动
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "平台上一共有多少为顾客？",
        "这个平台上总共有多少个买家？",
        "把所有曲风的名字列出来，按字母顺序排。",
        "公司现在有多少位同事？",
        "所有歌曲的单介合计是多少？四舍五入保留两位小数。",
        "２０２１ 年一共开了多少张单据？",
        "List 所有流派的名字，按 name 升序，顺便给出每个流派的 track 数量。",
        "麻烦帮我查一下哈，就是那个……2022 年一整年嘛，一共开了多少张发票呀？谢谢！",
        "哪位艺术佳给平台带来的收入最多？给我收入最高的 5 位，按名称升序排并列的。",
        "去年一共开了多少张发票？",
        "前年一共开了多少张发票？",
        "今年一共开了多少张发票？",
        "上个月一共开了多少张发票？",
        "这个月一共开了多少张发票？",
        "上个季度一共开了多少张发票？",
        "本季度一共开了多少张发票？",
        "客户总数是多少？",
    ],
)
def test_idempotency(q):
    once = norm(q)
    twice = normalize(once.normalized, time_anchor=ANCHOR)
    assert twice.changes == [], f"二次改写产生了新改动：{[c.model_dump() for c in twice.changes]}"


# ---------------------------------------------------------------------------
# C 层：语义兜底（§12，假 embedder 离线验证）
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """确定性假 embedder：mode=exact 时同串同向量、异串近正交（高维随机单位向量）。"""

    def __init__(self, mode: str = "exact", dim: int = 64):
        self.mode = mode
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        if self.mode == "constant":
            return [1.0] + [0.0] * (self.dim - 1)
        if self.mode == "orthogonal":
            return [0.0, 1.0] + [0.0] * (self.dim - 2)
        import hashlib

        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        rng = _DetRand(seed)
        v = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class _DetRand:
    """确定性伪随机（LCG），保证测试可复现。"""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def uniform(self, a: float, b: float) -> float:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return a + (b - a) * (self.state / 0x7FFFFFFF)


def _b_aliases() -> AliasTable:
    """B 层表：只有 track/歌曲。刻意**不含** 蓝调 —— 那是留给 C 层的词。"""
    return AliasTable(version="t", updated_at="t", tables={"track": ["歌曲"]})


def _semantic_source() -> AliasTable:
    """C 层候选来源：蓝调/爵士只出现在这里，用于验证「A/B 都没命中才轮到 C」。"""
    return AliasTable(
        version="t",
        updated_at="t",
        tables={"track": ["歌曲"]},
        values={"genre.name": {"Blues": ["蓝调"], "Jazz": ["爵士"]}},
    )


def test_semantic_layer_accepts_valid_candidate():
    """C 层只在 A/B 都没命中时生效：「蓝调」不在 B 层表里，语义兜底改写为 Blues。"""
    res = build_semantic_resolver(FakeEmbedder("exact"), aliases=_semantic_source())
    assert res._candidates, "候选矩阵不应为空"
    r = normalize("有多少首蓝调歌曲？", aliases=_b_aliases(), time_anchor=ANCHOR, semantic=res)
    assert "Blues" in r.normalized
    assert any(e.via == "semantic" and e.canonical == "Blues" for e in r.entities)
    assert "track" in canonicals(r, "table")  # 歌曲仍走 B 层，C 层不抢已命中的词


def test_semantic_layer_rejects_ambiguous():
    """top-1 与 top-2 分差不足（裕度 0.03）→ 不采纳，记 unresolved（§12.3 第 4 条）。"""
    res = build_semantic_resolver(FakeEmbedder("constant"), aliases=_semantic_source())
    r = normalize("有多少首蓝调歌曲？", aliases=_b_aliases(), time_anchor=ANCHOR, semantic=res)
    assert "Blues" not in r.normalized
    assert any("蓝调" in u for u in r.unresolved)


def test_semantic_layer_skips_below_threshold():
    """阈值提到 1.5（任何相似度都够不着）→ C 层整体沉默，不产生任何改写。"""
    res = build_semantic_resolver(FakeEmbedder("exact"), aliases=_semantic_source())
    res.threshold = 1.5
    r = normalize("有多少首蓝调歌曲？", aliases=_b_aliases(), time_anchor=ANCHOR, semantic=res)
    assert "Blues" not in r.normalized
    assert not any(e.via == "semantic" for e in r.entities)


def test_b_layer_wins_over_semantic():
    """「爵士」同时存在于 B 层值别名与 C 层候选：B 层（确定性）必须赢。"""
    res = build_semantic_resolver(FakeEmbedder("exact"), aliases=_semantic_source())
    r = normalize("有多少首爵士歌曲？", aliases=_semantic_source(), time_anchor=ANCHOR, semantic=res)
    assert "Jazz" in r.normalized
    assert any(e.via == "alias" and e.canonical == "Jazz" for e in r.entities)
    assert not any(e.via == "semantic" for e in r.entities)


# ---------------------------------------------------------------------------
# 配置：APP_TIME_ANCHOR（§6.1 / §十一.4）
# ---------------------------------------------------------------------------


def test_time_anchor_from_env(monkeypatch):
    monkeypatch.delenv("APP_TIME_ANCHOR", raising=False)
    assert time_anchor_from_env() is None
    monkeypatch.setenv("APP_TIME_ANCHOR", "2024-06-15")
    assert time_anchor_from_env() == datetime(2024, 6, 15)
    monkeypatch.setenv("APP_TIME_ANCHOR", "2024-13-99")
    with pytest.raises(ValueError):
        time_anchor_from_env()


# ---------------------------------------------------------------------------
# 编排层接入（§8）：先改写、再路由；改写步骤恒为 trace 第 1 步
# ---------------------------------------------------------------------------


def _mock_orchestrator():
    from app.orchestrator import build_default_orchestrator

    return build_default_orchestrator(
        graph_backend="mock", doc_search_backend="mock", nl2sql_backend="mock"
    )


def test_orchestrator_prepends_normalizer_step():
    o = _mock_orchestrator()
    resp = o.answer("这个平台上总共有多少个买家？")
    assert resp.trace, "trace 不应为空"
    first = resp.trace[0]
    assert first.tool == "normalizer"
    assert first.hit is None
    assert "customer" in first.output["normalized"]
    assert first.output["original"] == "这个平台上总共有多少个买家？"
    assert resp.need_clarify is False


def test_orchestrator_clarify_branch_keeps_normalizer_step():
    """§8.2 第 3 条：澄清分支也必须带上改写步骤，否则「改了但没留痕」。

    用「查一下」：改写层把 filler 清掉 → 标准问句变空 → 保持原句进入路由，
    长度 < 4 触发澄清。此时「帮我清掉了什么」必须留在 trace 里。
    """
    o = _mock_orchestrator()
    resp = o.answer("查一下")
    assert resp.need_clarify is True
    assert resp.trace and resp.trace[0].tool == "normalizer"
    assert resp.trace[0].output["original"] == "查一下"


def test_alias_table_loads_from_repo_file():
    table = load_alias_table()
    assert "customer" in table.tables and "买家" in table.tables["customer"]
    assert table.values["customer.country"]["USA"] == ["美国", "美利坚"]
