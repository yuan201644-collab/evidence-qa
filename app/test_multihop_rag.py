# -*- coding: utf-8 -*-
"""多跳检索（app/rag/multihop.py）的回归测试。

为什么必须用"造出来的假检索"来测，而不是跑真语料：
    这里要验的全是**结构性质**——步骤编号连不连续、重复片段有没有去重、
    合并后的重排用的是不是原问题、生成有没有只调一次、解析失败会不会静默退化。
    这些性质在真语料上看不出来（真语料只会给你一个"看起来对的答案"）。

跑法：
    D:/python/python.exe app/test_multihop_rag.py

覆盖：
    ① 开关关掉时，answer() 与单轮实现**逐字段一致**（默认路径零回归）
    ② 分解返回空 / JSON 坏 / LLM 抛错 —— 一律退化单查询，不抛错
    ③ 正常分解：两个子查询各跑一轮，步骤编号连续、合并去重正确
    ④ 跨轮重排用**原问题**（不是某个子查询），并按上限截断
    ⑤ 生成**只调一次**（这是引用编号不串的前提）
    ⑥ 充分性判断：字符串 "false" 不能当成 True（否则第二轮永远不触发且无声无息）
    ⑦ 引用语义不变：解析到 [n] 用子集，解析不到回退全部上下文
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["RETRIEVE_TOP_K"] = "20"
os.environ["RERANK_TOP_K"] = "5"
os.environ.pop("RAG_MULTIHOP", None)

from app.contracts import DocRef                                    # noqa: E402
from app.rag import answer as answer_mod                            # noqa: E402
from app.rag.multihop import (                                      # noqa: E402
    MultiHopRagTool,
    _as_bool,
    _binding_from_text,
    _complete_policy_chain_citations,
    _clean_plan_steps,
    _clean_queries,
    _dedup_hits,
    _extract_json_object,
    _applicable_policy_from_hits,
    _is_policy_question,
    _merge_refs,
    _named_subject_from_hits,
    _render_query_template,
    _service_route_requested,
    _equipment_route_requested,
)
from app.rag.schemas import Candidate, Chunk                        # noqa: E402

PASSED = 0


def check(name: str, fn) -> None:
    global PASSED
    try:
        fn()
    except AssertionError as e:
        print(f"[FAIL] {name}\n       {e}")
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {name}\n        {type(e).__name__}: {e}")
        raise
    PASSED += 1
    print(f"[ ok ] {name}")


# ── 语料池：A0 / A1 / B2 / B3，其中 A0 会在两个子查询里都命中（用来验去重） ──
POOL = [
    ("KB-001_维保合同", 1, ["第一章"], 100, "1.1　甲的备件时限：8 小时内到货。"),
    ("KB-001_维保合同", 2, ["第二章"], 200, "2.1　甲的违约金：千分之零点五。"),
    ("KB-013_机房运维操作规程", 1, [], 300, "乙的测试周期：每季度放电测试一次。"),
    ("KB-V01_A3机房综合运维手册", 1, ["2.2"], 400, "2.2　乙的记录：保存不少于 1 年。"),
]


def _hit(i):
    doc_id, page, section, cs, text = POOL[i]
    chunk = Chunk(doc_id=doc_id, text=text, page=page, section_path=section,
                  char_start=cs, char_end=cs + len(text))
    ref = DocRef(doc_id=doc_id, page=page, section_path="/".join(section),
                 snippet=text, char_start=cs, char_end=cs + len(text))
    return (Candidate(chunk=chunk, score=1.0, method="bm25"), ref)


class FakeHybrid:
    """按查询串里的字决定命中哪些片段；"乙"那一轮故意重复命中 A0。"""

    def __init__(self, bm25, store, embedder, k=60):
        self.k = k

    def search(self, query, top_k=None, doc_id=None):
        if "甲" in query:
            hits = [_hit(0), _hit(1)]
        elif "乙" in query:
            hits = [_hit(2), _hit(0)]        # ← 故意与"甲"轮重复 A0
        else:
            hits = [_hit(i) for i in range(len(POOL))]
        if doc_id:
            hits = [h for h in hits if h[1].doc_id == doc_id]
        return hits[:top_k] if top_k else hits


class FakeReranker:
    """确定性重排：整个列表倒序，方便验证"是否用原问题排的"。"""

    def __init__(self):
        self.queries = []

    def rerank(self, query, docs, top_n=None):
        self.queries.append(query)
        order = [(i, float(len(docs) - i)) for i in reversed(range(len(docs)))]
        return order[:top_n] if top_n else order


class ScriptedLLM:
    """按 system 提示词分流：分解 / 充分性 / 最终作答。并统计各自被调了几次。"""

    def __init__(self, decompose='{"subqueries": ["甲的备件时限", "乙的记录期限"]}',
                 sufficiency='{"sufficient": true}'):
        self.decompose = decompose
        self.sufficiency = sufficiency
        self.final = "据 [1] 与 [2]，甲的备件为 8 小时，乙的记录保存不少于 1 年。"
        self.n_decompose = 0
        self.n_sufficiency = 0
        self.n_final = 0
        self.final_user = None
        self.decompose_user = None

    def chat(self, system, user):
        if "检索规划" in system:
            self.n_decompose += 1
            self.decompose_user = user
            return self.decompose
        if "充分性" in system:
            self.n_sufficiency += 1
            return self.sufficiency
        self.n_final += 1
        self.final_user = user
        return self.final


def build(enabled=True, max_hops=2, final_k=8, llm=None, reranker=None):
    """造一个"真 RagAnswerTool + 假检索"的工具，再包成多跳。"""
    answer_mod.HybridRetriever = FakeHybrid
    llm = llm or ScriptedLLM()
    reranker = reranker or FakeReranker()
    rag = answer_mod.RagAnswerTool(bm25=None, store=None, embedder=None,
                                   llm=llm, reranker=reranker)
    tool = MultiHopRagTool(rag, enabled=enabled, max_subqueries=3,
                           max_hops=max_hops, final_context_top_k=final_k)
    return rag, tool, llm, reranker


def steps(res):
    return [(s.step_id, s.tool) for s in res.trace]


def _no_timing(dump):
    """去掉 elapsed_ms 再比。

    ⚠️ 为什么必须去掉：elapsed_ms 是计时值，两次调用**不可能**恰好相等。
    把它纳入逐字段比较，测试会随机失败（第一次跑恰好撞上计时器同一刻度才通过）。
    要验的不变量是「结构 + 内容一致」，不是「耗时一致」。
    """
    if isinstance(dump, dict):
        return {k: _no_timing(v) for k, v in dump.items() if k != "elapsed_ms"}
    if isinstance(dump, list):
        return [_no_timing(v) for v in dump]
    return dump


# ── ① 开关关掉 = 单轮 ────────────────────────────────────────────────
def t_disabled_identical_to_single_round():
    rag, tool, _llm, _rr = build(enabled=False)
    q = "甲和乙分别是什么？"
    single = rag.answer(q)
    multi = tool.answer(q)
    assert single.data == multi.data, "关闭时答案不一致"
    assert _no_timing([s.model_dump() for s in single.trace]) == \
        _no_timing([s.model_dump() for s in multi.trace]), "关闭时 trace 不一致"
    assert [r.model_dump() for r in single.refs] == [r.model_dump() for r in multi.refs], \
        "关闭时 refs 不一致"
    assert [t for _i, t in steps(multi)] == ["hybrid_retrieve", "rerank", "llm_generate"], \
        "关闭时不该出现任何多跳步骤，实际：" + str(steps(multi))


# ── ② 三种退化 ──────────────────────────────────────────────────────
def t_decompose_empty_falls_back():
    llm = ScriptedLLM(decompose='{"subqueries": []}')
    _rag, tool, _l, _r = build(llm=llm)
    res = tool.answer("甲和乙分别是什么？")
    tools = [t for _i, t in steps(res)]
    assert tools[0] == "decompose" and tools.count("hybrid_retrieve") == 1, \
        "分解为空时应只检索一轮，实际：" + str(tools)
    dec = res.trace[0]
    assert dec.output["fallback"] is True, "分解为空必须标记 fallback=True"


def t_decompose_bad_json_falls_back():
    for bad in ["不是 JSON", "", "```json\n{坏的}\n```"]:
        llm = ScriptedLLM(decompose=bad)
        _rag, tool, _l, _r = build(llm=llm)
        res = tool.answer("甲和乙分别是什么？")
        assert res.trace[0].output["fallback"] is True, f"坏输入未退化：{bad!r}"
        assert [t for _i, t in steps(res)].count("hybrid_retrieve") == 1, \
            f"坏输入应只检索一轮：{bad!r}"


def t_decompose_llm_raises_falls_back():
    class Boom(ScriptedLLM):
        def chat(self, system, user):
            if "检索规划" in system:
                raise RuntimeError("模拟 LLM 抖动")
            return super().chat(system, user)

    _rag, tool, _l, _r = build(llm=Boom())
    res = tool.answer("甲和乙分别是什么？")   # 不能抛错
    assert res.trace[0].output["fallback"] is True, "LLM 抛错时应退化"
    assert res.data, "退化后仍应给出答案"


# ── ③ 正常分解：编号连续 + 去重 ──────────────────────────────────────
def t_normal_two_subqueries_and_dedup():
    _rag, tool, llm, _rr = build()
    res = tool.answer("甲和乙分别是什么？")
    tools = [t for _i, t in steps(res)]
    assert tools.count("hybrid_retrieve") == 2, "两个子查询应各检索一次：" + str(tools)
    assert tools.count("rerank") == 2, "两个子查询应各重排一次：" + str(tools)

    ids = [i for i, _t in steps(res)]
    assert ids == list(range(1, len(ids) + 1)), "步骤编号必须连续递增，实际：" + str(ids)

    merge = [s for s in res.trace if s.tool == "context_merge"][0]
    assert merge.input["before"] == 4, "合并前应是 2+2=4 条，实际：" + str(merge.input)
    assert merge.output["after"] == 3, "A0 重复出现，去重后应为 3 条，实际：" + str(merge.output)
    assert llm.n_final == 1, "生成必须只调一次，实际 %d 次" % llm.n_final


# ── ③b 带变量计划：首跳证据绑定 -> 渲染第二跳 -> 保留两跳证据 ─────────
def t_dependent_plan_binds_and_renders_second_query():
    class DependentPlanLLM(ScriptedLLM):
        def __init__(self):
            super().__init__(decompose=(
                '{"steps":[{"query":"甲是谁？","bind":"person"},'
                '{"query_template":"{person}的备件时限是多少？"}]}'
            ))
            self.n_bind = 0

        def chat(self, system, user):
            if "实体抽取" in system:
                self.n_bind += 1
                return '{"value":"甲","citation_index":1}'
            return super().chat(system, user)

    llm = DependentPlanLLM()
    _rag, tool, _llm, _rr = build(llm=llm, final_k=3)
    res = tool.answer("甲的负责人负责什么？")
    tools = [name for _id, name in steps(res)]
    assert tools.count("hybrid_retrieve") == 2, "依赖计划应检索两跳：" + str(tools)
    assert tools.count("entity_bind") == 1, "首跳应抽取一次实体：" + str(tools)
    assert "plan_fallback" not in tools, "有证据的绑定不应退化：" + str(tools)
    bind = [s for s in res.trace if s.tool == "entity_bind"][0]
    assert bind.output["value"] == "甲" and len(bind.hit) == 1, "绑定值或证据不对"
    second = [s for s in res.trace if s.tool == "hybrid_retrieve"][1]
    assert second.input["query"] == "甲的备件时限是多少？", "第二跳未使用绑定实体"
    merge = [s for s in res.trace if s.tool == "context_merge"][0]
    assert merge.output["protected"] >= 2, "首跳和第二跳证据应受保护"
    provenance = [s for s in res.trace if s.tool == "provenance_complete"][0]
    assert provenance.output["final_refs"] >= 2, "最终溯源必须保留首跳绑定证据"
    assert llm.n_bind == 1 and llm.n_final == 1, "绑定和最终生成各应调用一次"


# ── ④ 跨轮重排用原问题 + 截断 ────────────────────────────────────────
def t_cross_rerank_uses_original_question_and_truncates():
    rr = FakeReranker()
    _rag, tool, _llm, _rr2 = build(final_k=2, reranker=rr)
    res = tool.answer("甲和乙分别是什么？")
    assert "甲和乙分别是什么？" in rr.queries, \
        "跨轮重排必须用原问题，实际收到的查询：" + str(rr.queries)
    cross = [s for s in res.trace if s.tool == "cross_rerank"][0]
    assert cross.input["num"] == 3, "跨轮重排应拿到去重后的 3 条"
    assert cross.output["kept"] == 2, "final_context_top_k=2 应只留 2 条"
    assert len(cross.hit) == 2
    # 进生成的片段数也必须同步变少（否则 prompt 里编号与实际不符）
    gen = [s for s in res.trace if s.tool == "llm_generate"][0]
    assert gen.input["context_num"] == 2, "生成用的片段数应与截断后一致"


# ── ⑤ 充分性：字符串 "false" 必须被当成假 ─────────────────────────────
def t_sufficiency_string_false_triggers_second_round():
    llm = ScriptedLLM(
        sufficiency='{"sufficient": "false", "next_queries": ["乙的记录期限"]}'
    )
    _rag, tool, _l, _rr = build(llm=llm, max_hops=2)
    res = tool.answer("甲和乙分别是什么？")
    tools = [t for _i, t in steps(res)]
    assert "sufficiency_check" in tools, "应出现充分性判断步骤：" + str(tools)
    # 首轮 2 个子查询 + 第二轮 1 个补充查询 = 3 次检索
    assert tools.count("hybrid_retrieve") == 3, \
        'sufficient="false" 必须触发第二轮，实际检索次数：%d' % tools.count("hybrid_retrieve")
    assert llm.n_sufficiency == 1, "最多问一次充分性（达 max_hops 后不再问）"


def t_sufficiency_true_stops():
    llm = ScriptedLLM(sufficiency='{"sufficient": true}')
    _rag, tool, _l, _rr = build(llm=llm, max_hops=2)
    res = tool.answer("甲和乙分别是什么？")
    tools = [t for _i, t in steps(res)]
    assert tools.count("hybrid_retrieve") == 2, "判为充分就不该再检索：" + str(tools)


def t_sufficiency_usable_false_when_unparsable():
    llm = ScriptedLLM(sufficiency="随便写点不是 JSON 的东西")
    _rag, tool, _l, _rr = build(llm=llm, max_hops=2)
    res = tool.answer("甲和乙分别是什么？")
    chk = [s for s in res.trace if s.tool == "sufficiency_check"][0]
    assert chk.output["usable"] is False, "解析失败必须标 usable=False"
    assert chk.output["sufficient"] is True, "解析失败按'够用'处理，不再多跳"


# ── ⑥ 引用语义不变 ──────────────────────────────────────────────────
def t_citation_subset_and_fallback():
    llm = ScriptedLLM()
    llm.final = "只引第一条 [1]。"
    _rag, tool, _l, _rr = build(llm=llm)
    res = tool.answer("甲和乙分别是什么？")
    assert len(res.refs) == 1, "解析到 [1] 时应只保留该条，实际 %d 条" % len(res.refs)

    llm2 = ScriptedLLM()
    llm2.final = "完全不写引用。"
    _rag2, tool2, _l2, _rr2 = build(llm=llm2)
    res2 = tool2.answer("甲和乙分别是什么？")
    gen = [s for s in res2.trace if s.tool == "llm_generate"][0]
    assert len(res2.refs) == gen.input["context_num"], \
        "没解析到引用时应回退为全部上下文（宁可偏多不可无据可查）"


def t_citation_numbers_match_compact_reference_list():
    llm = ScriptedLLM()
    llm.final = "第一和第三条分别支持这两个事实 [1]、[3]。"
    _rag, tool, _l, _rr = build(llm=llm)
    res = tool.answer("甲和乙分别是什么？")

    assert res.data.endswith("[1]、[2]。"), "压缩引用列表后正文编号也必须连续：" + res.data
    assert len(res.refs) == 2, "应只返回两个被引用片段"
    assert res.refs[0].doc_id.startswith("KB-013"), "[1] 应指向原上下文第一条"
    assert res.refs[1].doc_id.startswith("KB-001"), "[2] 应指向原上下文第三条"
    numbers = answer_mod._parse_citations(res.data, len(res.refs))
    assert numbers == [1, 2], "正文编号必须都能映射到返回的 refs：" + str(numbers)

    gen = [s for s in res.trace if s.tool == "llm_generate"][0]
    assert "<完整溯源要求>" in llm.final_user, "多跳生成必须收到完整关系链引用要求"
    assert "关系证据：" in llm.final_user, "答案必须单列可核验的关系证据"
    assert "其他对象、同类别对象或相邻制度的规定不能移用" in llm.final_user, \
        "生成提示必须禁止把其他对象的制度套用到本题"
    assert "不能绑定最终答案值" in llm.decompose_user, "规划不得绑定最终数值代替中间实体"
    assert gen.output["citation_fix"]["renumbered"] == {"1": 1, "3": 2}


def t_citation_markers_with_trailing_spaces_remain_mapped():
    text = "事实甲[编号 3 ]，事实乙[2 ]。"
    assert answer_mod._parse_citations(text, 3) == [3, 2]
    rewritten, changed = answer_mod._renumber_citations(text, {3: 1, 2: 2})
    assert rewritten == "事实甲[1]，事实乙[2]。"
    assert changed == {"3": 1, "2": 2}

    contexts = ["第一来源", "第二来源", "第三来源"]
    fixed, _details = answer_mod._fix_illegal_citations(
        "保留[2 ]和[编号 3 ]，删除越界[999999999999999999999999999999999999]。",
        contexts,
        len(contexts),
    )
    assert "[2 ]" in fixed and "[编号 3 ]" in fixed
    assert "999999999999999999999999999999999999" not in fixed
    assert answer_mod._parse_citations(fixed, len(contexts)) == [2, 3]
    assert answer_mod._parse_citations("非 ASCII 数字引用[٢]。", len(contexts)) == []


def t_chain_completion_remaps_trailing_space_citations():
    context_hits = [_hit(index) for index in range(3)]
    generated = {
        "text": "第三来源[1]，第二来源[2 ]。",
        "final_refs": [context_hits[2][1], context_hits[1][1]],
        "cite_fix": {"renumbered": {"3": 1, "2": 2}},
    }
    completed = _complete_policy_chain_citations(
        generated, context_hits, context_hits
    )
    assert completed["text"].startswith("第三来源[1]，第二来源[2]。"), completed["text"]
    assert [ref.doc_id for ref in completed["final_refs"]] == [
        context_hits[2][1].doc_id,
        context_hits[1][1].doc_id,
        context_hits[0][1].doc_id,
    ]
    assert answer_mod._parse_citations(completed["text"], len(completed["final_refs"])) == [1, 2, 3]


def t_merging_bridge_refs_does_not_shift_answer_citations():
    primary = [
        DocRef(doc_id="DOC-A", snippet="窄区间", char_start=0, char_end=5),
        DocRef(doc_id="DOC-A", snippet="宽区间", char_start=0, char_end=20),
        DocRef(doc_id="DOC-B", snippet="第二来源", char_start=10, char_end=18),
    ]
    bridge = [
        primary[1],
        DocRef(doc_id="DOC-C", snippet="桥接来源", char_start=4, char_end=12),
    ]
    merged = _merge_refs(primary, bridge)

    assert merged[:len(primary)] == primary, "答案已编号的主引用必须原序保留"
    assert len(merged) == 4, "同一文档、同一起点但区间不同的主引用不得折叠"
    assert merged[-1] == bridge[-1], "仅追加尚未出现的桥接来源"
    answer = "窄证据[1]，宽证据[2]，第二文档[3]。"
    for number, ref in enumerate(primary, start=1):
        assert merged[number - 1] == ref, f"[{number}] 必须仍指向原答案来源"
    assert answer_mod._parse_citations(answer, len(merged)) == [1, 2, 3]


# ── ⑦ 纯函数 ────────────────────────────────────────────────────────
def t_pure_helpers():
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object('废话 {"a": 1} 尾巴') == {"a": 1}
    assert _extract_json_object("不是 JSON") is None
    assert _extract_json_object("") is None
    assert _extract_json_object("[1, 2]") is None, "数组不算合法返回（要的是对象）"

    assert _as_bool("false") is False and _as_bool("true") is True
    assert _as_bool(0) is False and _as_bool(1) is True
    assert _as_bool(None, default=True) is True

    # 与原问题相同的条目要丢掉；重复要去掉；超上限要截断
    q = "甲和乙分别是什么？"
    got = _clean_queries([q, "甲的备件时限", "甲的备件时限", "乙的记录期限", "丙", "丁"], q, 2)
    assert got == ["甲的备件时限", "乙的记录期限"], "清洗结果不对：" + str(got)

    hits = [_hit(0), _hit(1), _hit(0)]
    assert len(_dedup_hits(hits)) == 2, "去重应把重复的 A0 去掉"

    plan = _clean_plan_steps(
        [{"query": "甲是谁", "bind": "person"}, {"query_template": "{person}负责什么"}],
        "问题", 3,
    )
    assert len(plan) == 2 and _render_query_template(plan[1]["text"], {"person": {"value": "甲"}}) == "甲负责什么"
    relation_chain = _clean_plan_steps(
        [
            {"query": "目标对象对应什么级别", "bind": "classification"},
            {"query_template": "目标对象适用{classification}下的什么制度", "bind": "policy"},
            {"query_template": "{policy}规定什么要求"},
        ],
        "目标对象的制度要求是什么", 3,
    )
    assert len(relation_chain) == 3, "对象→类别→制度→要求的三阶段计划应被接受"
    assert _clean_plan_steps([{"query_template": "{person}负责什么"}], "问题", 3) == []
    assert _clean_plan_steps([{"query": "甲是谁"}, {"query_template": "{person}负责什么"}], "问题", 3) == []
    assert _clean_plan_steps(
        [{"query": "对象保存多久", "bind": "deadline"},
         {"query_template": "期限{deadline}前如何处理"}], "问题", 3,
    ) == [], "最终期限不能作为检索绑定变量"
    assert _clean_plan_steps(
        [{"query": "对象是什么", "bind": "person"},
         {"query_template": "对象详情", "bind": "authorized_person"}], "问题", 3,
    ) == [], "没有后续消费的最终答案变量应被拒绝"

    person_hit = _hit(0)
    person_hit[0].chunk.text = "北区机房的机房主管为周宁。"
    got_binding = _binding_from_text([person_hit], "北区机房的负责人是谁")
    assert got_binding["value"] == "周宁" and got_binding["citation_index"] == 0

    distractor = _hit(1)
    distractor[0].chunk.text = "西部边缘机房的机房主管为顾晨。"
    assert not _binding_from_text([distractor], "东部算力中心的负责人是谁")["usable"]

    dataset_hit = _hit(0)
    dataset_hit[0].chunk.text = "# 设备能耗日汇总集数据目录\n设备能耗日汇总集定级为内部级L2。"
    relation_hit = _hit(1)
    relation_hit[0].chunk.text = (
        "# 内部级L2级数据管理范围\n设备能耗日汇总集（内部级L2）适用工业运行数据管理细则。"
    )
    policy_question = "设备能耗日汇总集的数据级别对应什么制度规定的保留时长？"
    assert _is_policy_question(policy_question)
    assert _is_policy_question("医保结算明细集应保留多久，访问这类数据有什么限制？")
    subject = _named_subject_from_hits(policy_question, [dataset_hit, relation_hit])
    assert subject == "设备能耗日汇总集", "必须从题目与命中目录中绑定具体对象"
    relation = _applicable_policy_from_hits(subject, [dataset_hit, relation_hit])
    assert relation == {"name": "工业运行数据管理细则", "citation_index": 1}
    assert _named_subject_from_hits("该数据集的保留期限是什么？", [dataset_hit, relation_hit]) == ""

    generated = {
        "text": "结果来自 [1]、[2]。",
        "final_refs": [_hit(0)[1], _hit(3)[1]],
        "cite_fix": {"renumbered": {"1": 1, "4": 2}},
    }
    complete = _complete_policy_chain_citations(
        generated, [_hit(i) for i in range(4)], [_hit(0), _hit(1), _hit(2)]
    )
    cited_numbers = answer_mod._parse_citations(complete["text"], len(complete["final_refs"]))
    assert len(complete["final_refs"]) == 4 and cited_numbers == [1, 2, 3, 4], \
        "强制补齐关系证据后，引用编号必须完整映射到来源列表"
    assert complete["cite_fix"]["policy_chain_completed"] is True

    chain_generated = {
        "text": "已核实周期。 [1]",
        "final_refs": [_hit(0)[1]],
        "cite_fix": {"renumbered": {"1": 1}},
    }
    complete_service = _complete_policy_chain_citations(
        chain_generated, [_hit(i) for i in range(4)], [_hit(0), _hit(1), _hit(2)],
        required_labels=["流程办理归属", "单位承接服务", "服务时限标准"],
        chain_name="service",
    )
    assert complete_service["cite_fix"]["service_chain_completed"] is True
    assert all(label in complete_service["text"] for label in ("流程办理归属", "单位承接服务", "服务时限标准"))
    assert _service_route_requested("轨交票务差错流程提交后首响和最终处理时限是什么")
    assert _equipment_route_requested("设备多久巡检一次，维护前需要什么安全操作")


def t_orchestrator_backend_switch():
    """三个后端值都能装配；多跳工具**注册同名工具**，编排层感知不到差别。

    为什么这条要测：如果将来有人改了 spec 的 name 或 capabilities，
    编排层的路由就会变，A/B 对比（rag vs rag_multihop）的"只换检索方式"前提就破了。
    """
    from app.orchestrator import build_default_orchestrator
    from app.tools.doc_search import RagDocSearchTool
    from app.tools.doc_search_multihop import MultiHopDocSearchTool

    rag_orch = build_default_orchestrator(doc_search_backend="rag")
    base = rag_orch.registry.require("doc_search")
    assert isinstance(base, RagDocSearchTool)

    mh_orch = build_default_orchestrator(doc_search_backend="rag_multihop")
    mh = mh_orch.registry.require("doc_search")
    assert isinstance(mh, MultiHopDocSearchTool)
    assert mh.spec.name == base.spec.name == "doc_search", "工具名变了，路由会跟着变"
    assert mh.spec.capabilities == base.spec.capabilities, "能力声明变了，路由会跟着变"
    assert mh.spec.input_schema == base.spec.input_schema, "入参 schema 变了，调用方会跟着变"

    mock_orch = build_default_orchestrator(doc_search_backend="mock")
    assert mock_orch.registry.require("doc_search") is not None

    try:
        build_default_orchestrator(doc_search_backend="乱写的值")
        raise AssertionError("非法后端值应当抛 ValueError")
    except ValueError as e:
        assert "DOC_SEARCH_BACKEND" in str(e), "报错信息里应指明是哪个变量"


def main() -> int:
    checks = [
        ("关闭开关时 answer() 与单轮实现逐字段一致", t_disabled_identical_to_single_round),
        ("分解返回空 -> 退化单查询", t_decompose_empty_falls_back),
        ("分解 JSON 坏 -> 退化单查询", t_decompose_bad_json_falls_back),
        ("分解时 LLM 抛错 -> 退化且不抛错", t_decompose_llm_raises_falls_back),
        ("正常分解：步骤编号连续 + 合并去重 + 生成只一次", t_normal_two_subqueries_and_dedup),
        ("依赖计划：首跳绑定实体并渲染第二跳查询", t_dependent_plan_binds_and_renders_second_query),
        ("跨轮重排用原问题并按上限截断", t_cross_rerank_uses_original_question_and_truncates),
        ('充分性 "false" -> 触发第二轮', t_sufficiency_string_false_triggers_second_round),
        ("充分性 true -> 不触发第二轮", t_sufficiency_true_stops),
        ("充分性不可解析 -> 不再多跳且标记 usable=False", t_sufficiency_usable_false_when_unparsable),
        ("引用：解析到用子集 / 解析不到回退全部", t_citation_subset_and_fallback),
        ("引用编号与压缩后的来源列表保持一致", t_citation_numbers_match_compact_reference_list),
        ("尾随空格与超长数字引用仍安全映射", t_citation_markers_with_trailing_spaces_remain_mapped),
        ("多跳补链正确重映射尾随空格引用", t_chain_completion_remaps_trailing_space_citations),
        ("补充桥接引用不改变答案既有编号映射", t_merging_bridge_refs_does_not_shift_answer_citations),
        ("纯函数（JSON 容错 / 布尔归一 / 查询清洗 / 去重）", t_pure_helpers),
        ("编排层三个后端都能装配、工具协议不变", t_orchestrator_backend_switch),
    ]
    for name, fn in checks:
        check(name, fn)
    print()
    print("ALL PASSED（%d 项）" % PASSED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
