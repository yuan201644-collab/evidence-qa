"""针对 B 的 09-21 意见做的回归测试。

这两个 bug 的共同特征是「**不报错，但结果是错的**」——最危险的一类。
所以必须用「模拟 B 的真实返回形状」来验，不能只用我自己的 mock 跑通就算数。

跑法：
    D:/python/python.exe app/test_b_feedback.py

覆盖：
    ① data 是纯字符串时，答案不能被丢掉（B 指出的形态差异）
    ② TraceStep.hit 挂全量召回时，final refs 只保留工具过滤过的那几条
    ③ 工具 refs 为空时才回退 trace 汇总（兜底路径不能坏）
    ④ 跨工具引用去重
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contracts import DocRef, SqlRef, ToolResult, TraceStep
from app.orchestrator import Orchestrator, ToolRun, _compose_one, _dedup_refs
from app.tools.base import BaseTool, ToolSpec
from app.tools.registry import ToolRegistry

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


# ── 一个复刻 B 的真实工具形状的假工具 ──────────────────────────────────
class FakeRagToolLikeB(BaseTool):
    """严格照 B 的 `RagAnswerTool.answer()` 的行为复刻：

    - `data` 是**纯字符串**（答案文本），不是 list[dict]
    - trace 三步：retrieve / rerank 挂**全量召回**（模拟 20 条），generate 挂重排后的 5 条
    - `refs` 是**过滤后**的（只保留答案里真的标了 [n] 的 2 条）

    这正是 B 说的「接上去会丢掉东西」的那两个形态差异。
    """

    #: 模拟「全量召回 20 条」
    RECALL = 20
    #: 模拟「重排后取 top 5」
    TOP_K = 5
    #: 模拟「答案实际引用了 2 条」
    CITED = 2

    def __init__(self) -> None:
        self._recall_refs = [
            DocRef(
                doc_id="KB-001_智能计算设备年度维保服务合同",
                doc_title="智能计算设备年度维保服务合同",
                page=i + 1,
                snippet=f"全量召回片段第 {i + 1} 条",
                char_start=i * 100,
                char_end=i * 100 + 50,
            )
            for i in range(self.RECALL)
        ]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="rag_like_b",
            capabilities=("unstructured_doc",),
            description="复刻 B 的 RAG 工具",
        )

    def run(self, payload):
        recall = self._recall_refs
        top_k = recall[: self.TOP_K]
        cited = top_k[: self.CITED]

        trace = [
            TraceStep(step_id=1, tool="hybrid_retrieve", elapsed_ms=1.0, hit=list(recall)),
            TraceStep(step_id=2, tool="rerank", elapsed_ms=1.0, hit=list(recall)),
            TraceStep(step_id=3, tool="llm_generate", elapsed_ms=1.0, hit=list(top_k)),
        ]
        # ⚠️ 关键：data 是**纯字符串**
        return ToolResult(
            data="根据现有资料，维保期为一年，单次响应时限为 4 小时[1]，"
            "年度巡检不少于 4 次[2]。",
            trace=trace,
            refs=list(cited),
        )


# ── ① data 是 str 时答案不能丢 ─────────────────────────────────────────
def t_str_data_not_lost() -> None:
    # 直接测拼装函数
    text = _compose_one("这是一段 RAG 答案文本")
    assert text == "这是一段 RAG 答案文本", f"str 形态没接住：{text!r}"

    # 端到端：走完整编排链路
    reg = ToolRegistry()
    reg.register(FakeRagToolLikeB())

    # 这个 mock 工具的 spec 名与注册名一致即可；路由用文本触发
    from app.orchestrator import route

    plan = route("维保期是怎么规定的", reg)
    assert [n for n, _ in plan.steps] == ["rag_like_b"], plan.steps

    orch = Orchestrator(reg, graph_backend="mock")
    resp = orch.answer("维保期是怎么规定的", "s-b1")

    assert resp.answer, "答案不能为空"
    assert "已执行查询，但没有取到可用的结果" not in resp.answer, (
        "❌ 命中 B 指出的 bug：data 是 str 时答案被丢掉"
    )
    assert "维保期为一年" in resp.answer, resp.answer


# ── ② final refs 只保留工具过滤过的那几条 ──────────────────────────────
def t_refs_prefer_tool_refs() -> None:
    reg = ToolRegistry()
    tool = FakeRagToolLikeB()
    reg.register(tool)

    orch = Orchestrator(reg, graph_backend="mock")
    resp = orch.answer("维保期是怎么规定的", "s-b2")

    # normalizer 与语义澄清留痕后，再加 3 步 RAG trace，共 5 步
    assert len(resp.trace) == 5, f"三步 RAG trace + 两步前置留痕应在，实际 {len(resp.trace)}"
    assert [step.tool for step in resp.trace[:2]] == ["normalizer", "semantic_clarifier"]
    assert resp.trace[0].tool == "normalizer" and resp.trace[0].hit is None
    # trace.hit 里确实挂着 20 条全量召回（证明这个场景是真实的）
    recall_in_trace = {r.doc_id + str(r.char_start) for s in resp.trace for r in (s.hit or [])}
    assert len(recall_in_trace) >= FakeRagToolLikeB.RECALL, (
        f"前置条件不成立：trace 里应有全量召回，实际 {len(recall_in_trace)} 条"
    )

    # 但 final refs 必须只有「答案实际引用的」那几条
    assert len(resp.refs) == FakeRagToolLikeB.CITED, (
        f"❌ 命中 B 指出的 bug：应按工具过滤后的 {FakeRagToolLikeB.CITED} 条，"
        f"实际给了 {len(resp.refs)} 条（等于把全量召回当引用了）"
    )
    # 且必须是 top_k 里被引用的那两条，不是乱序取的
    assert resp.refs[0].char_start == 0
    assert resp.refs[1].char_start == 100


# ── ③ 工具 refs 为空时回退 trace 汇总 ──────────────────────────────────
def t_fallback_to_collect_refs() -> None:
    class NoRefsTool(BaseTool):
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="no_refs",
                capabilities=("unstructured_doc",),
                description="refs 为空的工具",
            )

        def run(self, payload):
            ref = DocRef(doc_id="KB-002_某制度", page=1, snippet="兜底片段")
            step = TraceStep(step_id=1, tool="no_refs", elapsed_ms=1.0, hit=[ref])
            # refs 故意留空
            return ToolResult(data="某答案", trace=[step], refs=[])

    reg = ToolRegistry()
    reg.register(NoRefsTool())
    from app.orchestrator import route

    plan = route("某制度是怎么规定的", reg)
    assert [n for n, _ in plan.steps] == ["no_refs"], plan.steps

    orch = Orchestrator(reg)
    resp = orch.answer("某制度是怎么规定的", "s-b3")
    assert len(resp.refs) == 1, f"兜底路径坏了：应回退 trace 汇总拿到 1 条，实际 {len(resp.refs)}"


# ── ④ 跨工具去重 ───────────────────────────────────────────────────────
def t_cross_tool_dedup() -> None:
    same = DocRef(doc_id="KB-003_x", page=2, section_path="2.1", snippet="同一条")
    sql = SqlRef(table="t", column="c", row_pk="1", snippet="结构化那条")

    out = _dedup_refs([same, sql, same])
    assert len(out) == 2, f"跨工具去重失败：应 2 条，实际 {len(out)}"


# ── ⑤ 保留既有形态不回归 ───────────────────────────────────────────────
def t_existing_shapes_still_work() -> None:
    # list[dict]
    assert _compose_one([{"name": "李伟", "total": 100}]) == "name：李伟、total：100"
    # dict 带 text
    assert _compose_one({"text": "一段话"}) == "一段话"
    # dict 带 conclusion
    assert _compose_one({"conclusion": "结论句"}) == "结论句"
    # list[str]
    assert _compose_one(["甲", "乙"]) == "甲；乙"
    # 空 / 认不出的形态
    assert _compose_one([]) == ""
    assert _compose_one(None) == ""
    assert _compose_one(12345) == ""


if __name__ == "__main__":
    tests = [
        ("① data 是 str 时答案不被丢（B 的形态差异）", t_str_data_not_lost),
        ("② final refs 优先取工具过滤后的（B 的语义差异）", t_refs_prefer_tool_refs),
        ("③ 工具 refs 为空时回退 trace 汇总", t_fallback_to_collect_refs),
        ("④ 跨工具引用去重", t_cross_tool_dedup),
        ("⑤ 既有四种形态不回归", t_existing_shapes_still_work),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
