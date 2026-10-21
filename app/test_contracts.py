"""契约自检（contract tests）。

作用：证明 `app/contracts.py` 的字段能承住一次完整的问答流程，
并且把「踩过的坑」固化成断言，防止将来改回去。

跑法（项目根目录下）：
    D:/python/python.exe app/test_contracts.py

全部通过时最后打印 `ALL PASSED`，任一失败会抛 AssertionError。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

from app.contracts import (
    AskRequest,
    AskResponse,
    DocRef,
    FactReview,
    FactReviewItem,
    SqlRef,
    ToolResult,
    TraceStep,
    collect_refs,
    merge_trace,
)

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


# ── 1. 两种引用都能建，且子类字段不丢 ──────────────────────────────────────
def t_sql_ref() -> None:
    r = SqlRef(table="orders", column="total_amount", row_pk="10248", snippet="{amount: 3200}")
    assert r.kind == "sql"
    assert r.table == "orders"
    assert r.column == "total_amount"
    assert r.row_pk == "10248"


def t_doc_ref() -> None:
    r = DocRef(doc_id="报销制度.pdf", page=12, section_path="第 3 章 > 3.2 报销标准", snippet="单次上限 5000 元")
    assert r.kind == "doc"
    assert r.page == 12


# ── 2. 硬约束：snippet 必须非空（对应评测口径 L200）──────────────────────
def t_snippet_required() -> None:
    try:
        DocRef(doc_id="x", snippet="")
    except ValidationError:
        return
    raise AssertionError("空的 snippet 竟然通过了，评测口径要求引用片段非空")


def t_snippet_missing() -> None:
    try:
        DocRef(doc_id="x")
    except ValidationError:
        return
    raise AssertionError("不传 snippet 竟然通过了")


# ── 3. 必填字段缺失要报错 ────────────────────────────────────────────────
def t_table_required() -> None:
    try:
        SqlRef(snippet="x")
    except ValidationError:
        return
    raise AssertionError("SqlRef 缺 table 竟然通过了")


def t_doc_id_required() -> None:
    try:
        DocRef(snippet="x")
    except ValidationError:
        return
    raise AssertionError("DocRef 缺 doc_id 竟然通过了")


# ── 4. 类型写错要报错（pydantic 真正保护你的地方）─────────────────────────
def t_type_enforced() -> None:
    try:
        DocRef(doc_id="x", snippet="y", page="十二")
    except ValidationError:
        return
    raise AssertionError('page="十二" 竟然通过了')


def t_extra_forbidden() -> None:
    """本项目显式设了 extra='forbid'，所以拼错字段名会报错。

    注意：这是**本项目的配置**，不是 pydantic 的默认行为。
    pydantic v2 默认会静默忽略多余字段（已实测），本项目选择让它报错。
    """
    try:
        SqlRef(table="orders", snippet="x", tabel="typo")
    except ValidationError:
        return
    raise AssertionError("多余字段竟然通过了——extra='forbid' 没生效")


# ── 5. 判别式生效：混装列表的字段不丢 ────────────────────────────────────
def t_discriminator() -> None:
    resp = AskResponse(
        answer="3200 万",
        refs=[
            SqlRef(table="orders", column="total_amount", snippet="amount=3200"),
            DocRef(doc_id="手册.pdf", page=12, snippet="上限 5000 元"),
        ],
    )
    dumped = resp.model_dump()
    assert dumped["refs"][0]["table"] == "orders", "SqlRef.table 丢了"
    assert dumped["refs"][1]["page"] == 12, "DocRef.page 丢了（判别式没生效）"
    assert dumped["refs"][1]["doc_id"] == "手册.pdf", "DocRef.doc_id 丢了"


# ── 6. JSON 往返（序列化）────────────────────────────────────────────────
def t_round_trip() -> None:
    resp = AskResponse(
        answer="上限 5000 元",
        refs=[DocRef(doc_id="手册.pdf", page=12, snippet="上限 5000 元")],
    )
    raw = resp.model_dump_json()
    back = AskResponse.model_validate_json(raw)
    assert back.refs[0].snippet == "上限 5000 元"
    assert getattr(back.refs[0], "page", None) == 12


# ── 7. trace 拼接：step_id 重排，tool 标记保留 ───────────────────────────
def t_merge_trace() -> None:
    a = [
        TraceStep(step_id=7, tool="nl2sql", input={"q": "x"}, output={}, elapsed_ms=12.0),
        TraceStep(step_id=9, tool="nl2sql", input={"q": "x"}, output={}, elapsed_ms=3.0),
    ]
    b = [TraceStep(step_id=1, tool="rag", input={}, output={}, elapsed_ms=8.0)]
    merged = merge_trace(a + b)
    assert [s.step_id for s in merged] == [1, 2, 3], "step_id 没有重排"
    assert {s.tool for s in merged} == {"nl2sql", "rag"}, "tool 标记丢了，分不清归属"


# ── 8. refs 汇总与去重 ──────────────────────────────────────────────────
def t_collect_refs() -> None:
    r1 = SqlRef(table="orders", column="amount", snippet="a")
    r1_dup = SqlRef(table="orders", column="amount", snippet="a")  # 同一条
    r2 = DocRef(doc_id="d.pdf", page=1, snippet="b")
    trace = [
        TraceStep(step_id=1, tool="nl2sql", input={}, output={}, elapsed_ms=1.0, hit=[r1]),
        TraceStep(step_id=2, tool="nl2sql", input={}, output={}, elapsed_ms=1.0, hit=[r1_dup, r2]),
    ]
    refs = collect_refs(trace)
    assert len(refs) == 2, f"去重失败，期望 2 条，实得 {len(refs)}"
    assert refs[0].kind == "sql"
    assert refs[1].kind == "doc"


def t_collect_refs_keeps_distinct_query_result_evidence() -> None:
    first = SqlRef(
        table="invoices", column="total", result_id="query-a", snippet="总额 10",
    )
    duplicate = first.model_copy(update={"snippet": "同一结果的另一段说明"})
    second = SqlRef(
        table="invoices", column="total", result_id="query-b", snippet="总额 20",
    )
    trace = [TraceStep(step_id=1, tool="nl2sql", elapsed_ms=1, hit=[first, duplicate, second])]

    refs = collect_refs(trace)

    assert refs == [first, second]


# ── 9. char_start / char_end 字符偏移（v0.2 新增）────────────────────────
def t_doc_ref_char_offset() -> None:
    """偏移可填可不填；填了要能存住，不填要是 None 而不是报错。"""
    with_off = DocRef(
        doc_id="报销制度.pdf", page=12, snippet="单次上限 5000 元",
        char_start=1205, char_end=1230,
    )
    assert with_off.char_start == 1205
    assert with_off.char_end == 1230

    without_off = DocRef(doc_id="报销制度.pdf", page=12, snippet="单次上限 5000 元")
    assert without_off.char_start is None
    assert without_off.char_end is None

    # 负偏移应被拒（ge=0）
    try:
        DocRef(doc_id="x.pdf", snippet="y", char_start=-1)
    except ValidationError:
        return
    raise AssertionError("负的 char_start 竟然通过了")


def t_collect_refs_by_offset() -> None:
    """有偏移时按偏移去重：同一页的不同段落要能区分开。

    这是 v0.2 的核心收益——v0.1 只按 (doc_id, page, section_path) 去重，
    同页不同段落会被误判为同一条。
    """
    same_page = dict(doc_id="手册.pdf", page=3, section_path="第 2 章")
    r1 = DocRef(snippet="第一段", char_start=100, char_end=130, **same_page)
    r2 = DocRef(snippet="第二段", char_start=200, char_end=240, **same_page)
    r1_dup = DocRef(snippet="第一段", char_start=100, char_end=130, **same_page)

    trace = [
        TraceStep(step_id=1, tool="rag_search", input={}, output={}, elapsed_ms=1.0,
                  hit=[r1, r2]),
        TraceStep(step_id=2, tool="rag_search", input={}, output={}, elapsed_ms=1.0,
                  hit=[r1_dup]),
    ]
    refs = collect_refs(trace)
    assert len(refs) == 2, f"同页两段应各算一条，实得 {len(refs)} 条"
    assert [r.snippet for r in refs] == ["第一段", "第二段"]


def t_collect_refs_no_offset_fallback() -> None:
    """没有偏移时退回老逻辑：同页同章节仍算同一条（保持向后兼容）。"""
    r1 = DocRef(doc_id="手册.pdf", page=3, section_path="第 2 章", snippet="甲")
    r2 = DocRef(doc_id="手册.pdf", page=3, section_path="第 2 章", snippet="乙")
    trace = [
        TraceStep(step_id=1, tool="rag_search", input={}, output={}, elapsed_ms=1.0,
                  hit=[r1, r2]),
    ]
    refs = collect_refs(trace)
    assert len(refs) == 1, f"无偏移时应按老逻辑合并为 1 条，实得 {len(refs)} 条"


# ── 10. 澄清场景：复用 answer 装反问句 ───────────────────────────────────
def t_clarify() -> None:
    resp = AskResponse(answer="您指的是哪一年的销售额？", need_clarify=True)
    assert resp.need_clarify is True
    assert "哪一年" in resp.answer
    assert resp.refs == []


# ── 11. ToolResult 三字段 ───────────────────────────────────────────────
def t_tool_result() -> None:
    tr = ToolResult(
        data=[{"year": 2025, "amount": 3200}],
        trace=[TraceStep(step_id=1, tool="nl2sql", input={}, output={}, elapsed_ms=5.0)],
        refs=[SqlRef(table="orders", snippet="amount=3200")],
    )
    assert tr.data[0]["amount"] == 3200
    assert len(tr.trace) == 1
    assert len(tr.refs) == 1


# ── 12. HTTP 请求体 ─────────────────────────────────────────────────────
def t_ask_request() -> None:
    req = AskRequest(question="去年销售额多少", session_id="s-001")
    assert req.question == "去年销售额多少"
    try:
        AskRequest(question="", session_id="s-001")
    except ValidationError:
        return
    raise AssertionError("空 question 竟然通过了")


def t_fact_review_round_trip() -> None:
    review = FactReview(
        coverage="cross_source_work_order_sla",
        verification_mode="strict",
        verification_scope="fact_dependencies_and_business_checks",
        scope_note="仅展示事实账本，不代表答案全文逐句核验。",
        items=[FactReviewItem(
            fact_id="sla_verdict",
            label="修复时限判断",
            status="supported",
            execution_status="completed",
            evidence_link_status="complete",
            checks=[{"code": "service_restored_within_limit", "result": "passed"}],
            evidence_ref_indices=[0],
        )],
    )
    response = AskResponse(
        answer="受控回答",
        refs=[SqlRef(table="work_orders", row_pk="WO-1", snippet="WO-1")],
        fact_review=review,
    )
    back = AskResponse.model_validate_json(response.model_dump_json())
    assert back.fact_review is not None
    assert back.fact_review.items[0].evidence_ref_indices == [0]
    assert back.fact_review.verification_mode == "strict"
    assert AskResponse(answer="legacy").fact_review is None


def t_fact_review_forbids_unknown_fields() -> None:
    try:
        FactReview(
            coverage="cross_source_work_order_sla",
            verification_mode="strict",
            verification_scope="fact_dependencies_and_business_checks",
            scope_note="scope",
            items=[],
            made_up=True,
        )
    except ValidationError:
        return
    raise AssertionError("FactReview 未拒绝额外字段")


def t_fact_review_reference_indices_must_fit_response_refs() -> None:
    try:
        AskResponse(
            answer="候选回答",
            refs=[],
            fact_review=FactReview(
                coverage="structured_query",
                verification_mode="source_specific",
                verification_scope="query_execution_and_evidence",
                scope_note="仅记录查询执行，不代表答案语义已核验。",
                items=[FactReviewItem(
                    fact_id="structured_query_target",
                    label="本次数据查询目标",
                    status="candidate",
                    execution_status="completed",
                    evidence_link_status="complete",
                    evidence_ref_indices=[0],
                )],
            ),
        )
    except ValidationError:
        return
    raise AssertionError("越界的事实证据索引竟然通过 AskResponse 校验")


def t_fact_review_reference_indices_must_be_unique() -> None:
    try:
        AskResponse(
            answer="候选回答",
            refs=[SqlRef(table="invoices", column="total", result_id="query-1", snippet="总额")],
            fact_review=FactReview(
                coverage="structured_query",
                verification_mode="source_specific",
                verification_scope="query_execution_and_evidence",
                scope_note="仅记录查询执行，不代表答案语义已核验。",
                items=[FactReviewItem(
                    fact_id="structured_query_target",
                    label="本次数据查询目标",
                    status="candidate",
                    execution_status="completed",
                    evidence_link_status="complete",
                    evidence_ref_indices=[0, 0],
                )],
            ),
        )
    except ValidationError:
        return
    raise AssertionError("重复的事实证据索引竟然通过 AskResponse 校验")


# ── 13. 几何高亮字段（v0.3 新增）────────────────────────────────────────
def t_doc_ref_bboxes() -> None:
    """bboxes 可填可不填；填了要存住，不填要是空列表。

    用处：前端拿 page + bboxes + page_width/height 画 PDF 高亮框。
    """
    r = DocRef(
        doc_id="1", doc_title="员工报销制度", page=12, snippet="单次上限 5000 元",
        bboxes=[(72.0, 100.0, 300.0, 120.0), (72.0, 130.0, 280.0, 150.0)],
        page_width=595.28, page_height=841.89,
    )
    assert r.doc_title == "员工报销制度"
    assert len(r.bboxes) == 2
    assert r.bboxes[0] == (72.0, 100.0, 300.0, 120.0)
    assert r.page_width == 595.28
    assert r.page_height == 841.89

    empty = DocRef(doc_id="1", snippet="x")
    assert empty.bboxes == [], "不传时应是空列表，不是 None"
    assert empty.page_width is None
    assert empty.page_height is None
    assert empty.doc_title is None


def t_bboxes_arity() -> None:
    """bboxes 每个元素必须恰好 4 个数 —— 这是用 tuple 收窄的意义。

    写成 list[list] 的话，(1,2,3) 或 (1,2,3,4,5) 都能悄悄溜进去，
    前端拿到才发现画不出框。类型上锁死，错误当场暴露。
    """
    for bad in [(1.0, 2.0, 3.0), (1.0, 2.0, 3.0, 4.0, 5.0), (), (1.0,)]:
        try:
            DocRef(doc_id="1", snippet="x", bboxes=[bad])
        except ValidationError:
            continue
        raise AssertionError(f"非 4 元组 {bad} 竟然通过了")


def t_page_size_positive() -> None:
    """页面尺寸必须为正数（gt=0）：0 或负值会让前端换算除零 / 反向。"""
    for bad in [(0.0, 100.0), (-1.0, 100.0), (100.0, 0.0), (100.0, -5.0)]:
        try:
            DocRef(doc_id="1", snippet="x", page_width=bad[0], page_height=bad[1])
        except ValidationError:
            continue
        raise AssertionError(f"非正页面尺寸 {bad} 竟然通过了")


def t_bboxes_round_trip() -> None:
    """JSON 往返不丢几何坐标；混装 SqlRef 时也不能丢（判别式生效）。

    注意 model_dump() 出来是 Python 对象（tuple），model_dump_json() 再
    解析回来会变回 tuple —— 两处都要验，因为前端拿到的是 JSON。
    """
    ref = DocRef(
        doc_id="1", doc_title="制度", page=3, snippet="x",
        bboxes=[(1.0, 2.0, 3.0, 4.0)], page_width=595.0, page_height=842.0,
    )

    # ① 直接 dump（Python 侧）
    dumped = ref.model_dump()
    assert dumped["bboxes"] == [(1.0, 2.0, 3.0, 4.0)], dumped["bboxes"]
    assert dumped["page_width"] == 595.0
    assert dumped["doc_title"] == "制度"

    # ② JSON 往返（前端实际拿到的形态）
    back = DocRef.model_validate_json(ref.model_dump_json())
    assert back.bboxes == [(1.0, 2.0, 3.0, 4.0)], "JSON 往返后 bboxes 变了"
    assert back.page_width == 595.0
    assert back.doc_title == "制度"

    # ③ 混装 SqlRef，doc 侧新字段不能丢
    resp = AskResponse(
        answer="a",
        refs=[SqlRef(table="orders", snippet="s"), ref],
    )
    dd = resp.model_dump()
    assert dd["refs"][1]["bboxes"] == [(1.0, 2.0, 3.0, 4.0)], "混装时 bboxes 丢了"
    assert dd["refs"][1]["doc_title"] == "制度", "混装时 doc_title 丢了"
    assert dd["refs"][1]["page_width"] == 595.0, "混装时 page_width 丢了"


# ── 14. 端到端：一次完整问答（mock 链路）────────────────────────────────
def t_extraction_method_optional() -> None:
    """旧写法构造 DocRef（不传 extraction_method）必须仍然通过。

    这是加字段的**兼容性直接证据**：仓库里已有大量 DocRef(...) 调用点
    （B 侧检索、C 侧评测、各处测试），新字段必须可选、默认 None，
    否则一次契约变更会把别人的代码全弄挂。
    """
    old = DocRef(doc_id="KB-001_维保合同", page=2, snippet="质保期 36 个月")
    assert old.extraction_method is None, "不传时应为 None"

    marked = DocRef(doc_id="KB-007_巡检扫描件", page=1, snippet="压差值 8Pa",
                    extraction_method="ocr")
    assert marked.extraction_method == "ocr"


def t_extraction_method_values() -> None:
    """取值只能是三个字面量；写错当场报错，不留到前端才发现。"""
    for good in ("ocr", "text_layer", "plain"):
        r = DocRef(doc_id="x", snippet="y", extraction_method=good)
        assert r.extraction_method == good
    for bad in ("OCR", "textlayer", "table", "pdf", ""):
        try:
            DocRef(doc_id="x", snippet="y", extraction_method=bad)
        except ValidationError:
            continue
        raise AssertionError(f"非法取值 {bad!r} 竟然通过了")


def t_extraction_method_round_trip() -> None:
    """JSON 往返不丢提取方式（前端拿到的是 JSON）。"""
    ref = DocRef(doc_id="KB-007_服务器巡检记录扫描件", page=1, snippet="x",
                 extraction_method="ocr")
    back = DocRef.model_validate_json(ref.model_dump_json())
    assert back.extraction_method == "ocr", "JSON 往返后丢了 extraction_method"
    assert back == ref


def t_extraction_method_not_in_dedup() -> None:
    """提取方式**不参与去重**：定位相同的两条算同一条，来源不同也一样。

    契约约定「非定位字段取首次出现那条」。若把 extraction_method 塞进去重 key，
    同一段证据走两条通道（文本层 / OCR）就会被拆成两条，引用卡片出现重复项。
    """
    steps = [
        TraceStep(step_id=1, tool="bm25", input={}, output={}, elapsed_ms=1.0, hit=[
            DocRef(doc_id="KB-001", page=3, char_start=10, char_end=20,
                   snippet="首次出现", extraction_method="text_layer"),
        ]),
        TraceStep(step_id=2, tool="vector", input={}, output={}, elapsed_ms=1.0, hit=[
            DocRef(doc_id="KB-001", page=3, char_start=10, char_end=20,
                   snippet="第二次出现", extraction_method="ocr"),
        ]),
    ]
    refs = collect_refs(steps)
    assert len(refs) == 1, f"同一段证据应合并为 1 条，实际 {len(refs)} 条"
    assert refs[0].snippet == "首次出现", "应保留首次出现的 snippet"
    assert refs[0].extraction_method == "text_layer", "非定位字段同样取首次出现那条"


def t_end_to_end() -> None:
    """模拟编排层：收到提问 → 调两个工具 → 汇总成响应。

    这是「在 mock 链路上跑通一次完整问答」的最小复现。
    """
    req = AskRequest(question="去年销售额多少？报销上限是多少？", session_id="s-002")

    sql_ref = SqlRef(table="orders", column="total_amount", snippet="total_amount=3200")
    doc_ref = DocRef(
        doc_id="1", doc_title="员工报销制度", page=12, snippet="单次报销上限 5000 元",
        char_start=1205, char_end=1230,
        # v0.3：几何高亮三件套（前端靠它们画框）
        bboxes=[(72.0, 300.0, 400.0, 320.0)],
        page_width=595.28, page_height=841.89,
    )

    tool_a = ToolResult(
        data=[{"total": 3200}],
        trace=[
            TraceStep(step_id=1, tool="nl2sql", input={"q": req.question},
                      output={"rows": 1}, elapsed_ms=120.5, hit=[sql_ref]),
        ],
        refs=[sql_ref],
    )
    tool_b = ToolResult(
        data=[{"text": "单次报销上限 5000 元"}],
        trace=[
            TraceStep(step_id=1, tool="rag_search", input={"q": req.question},
                      output={"hits": 1}, elapsed_ms=88.2, hit=[doc_ref]),
        ],
        refs=[doc_ref],
    )

    all_steps = tool_a.trace + tool_b.trace
    resp = AskResponse(
        answer="去年销售额 3200 万元；单次报销上限 5000 元。",
        trace=merge_trace(all_steps),
        refs=collect_refs(all_steps),
        sql="SELECT SUM(total_amount) FROM orders WHERE year = 2025",
        need_clarify=False,
    )

    # 不变量检查
    assert len(resp.trace) == 2
    assert [s.step_id for s in resp.trace] == [1, 2]
    assert len(resp.refs) == 2, "两条引用应都在"
    assert all(r.snippet for r in resp.refs), "snippet 不得为空（评测口径 L200）"
    assert {s.tool for s in resp.trace} == {"nl2sql", "rag_search"}

    # 序列化往返
    back = AskResponse.model_validate_json(resp.model_dump_json())
    assert len(back.refs) == 2
    assert getattr(back.refs[1], "doc_id", None) == "1"
    # v0.3：几何高亮字段也要能过 JSON 往返
    assert getattr(back.refs[1], "bboxes", None) == [(72.0, 300.0, 400.0, 320.0)]
    assert getattr(back.refs[1], "page_width", None) == 595.28
    assert getattr(back.refs[1], "doc_title", None) == "员工报销制度"
    print("       端到端响应 refs:", [(r.kind, r.snippet) for r in back.refs])


if __name__ == "__main__":
    tests = [
        ("SqlRef 建得出来", t_sql_ref),
        ("DocRef 建得出来", t_doc_ref),
        ("snippet 空字符串被拒", t_snippet_required),
        ("snippet 缺失被拒", t_snippet_missing),
        ("SqlRef 缺 table 被拒", t_table_required),
        ("DocRef 缺 doc_id 被拒", t_doc_id_required),
        ("类型写错被拒", t_type_enforced),
        ("多余字段被拒（extra=forbid）", t_extra_forbidden),
        ("判别式生效：混装字段不丢", t_discriminator),
        ("JSON 往返不丢字段", t_round_trip),
        ("trace 拼接：step_id 重排", t_merge_trace),
        ("refs 汇总去重", t_collect_refs),
        ("查询结果级证据保留独立定位", t_collect_refs_keeps_distinct_query_result_evidence),
        ("字符偏移可填可不填", t_doc_ref_char_offset),
        ("有偏移：同页两段各算一条", t_collect_refs_by_offset),
        ("无偏移：退回老逻辑合并", t_collect_refs_no_offset_fallback),
        ("几何高亮字段可填可不填", t_doc_ref_bboxes),
        ("bboxes 非 4 元组被拒", t_bboxes_arity),
        ("页面尺寸必须为正数", t_page_size_positive),
        ("几何字段 JSON 往返不丢", t_bboxes_round_trip),
        ("旧写法构造 DocRef 仍通过（兼容）", t_extraction_method_optional),
        ("提取方式取值受限", t_extraction_method_values),
        ("提取方式 JSON 往返不丢", t_extraction_method_round_trip),
        ("提取方式不参与去重", t_extraction_method_not_in_dedup),
        ("澄清：复用 answer", t_clarify),
        ("ToolResult 三字段", t_tool_result),
        ("HTTP 请求体校验", t_ask_request),
        ("逐事实核验契约 JSON 往返", t_fact_review_round_trip),
        ("逐事实核验契约拒绝未知字段", t_fact_review_forbids_unknown_fields),
        ("逐事实核验索引必须落在最终 refs 内", t_fact_review_reference_indices_must_fit_response_refs),
        ("逐事实核验索引不能重复", t_fact_review_reference_indices_must_be_unique),
        ("端到端 mock 链路", t_end_to_end),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
