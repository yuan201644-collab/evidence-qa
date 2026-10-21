"""真实 doc_search 适配层自检；使用 fake RAG，不加载模型或访问网络。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contracts import ToolResult, TraceStep
from app.orchestrator import build_default_orchestrator, route
from app.tools.doc_search import RagDocSearchTool


class FakeRagTool:
    def __init__(self):
        self.questions = []

    def answer(self, question):
        self.questions.append(question)
        return ToolResult(
            data="真实 RAG 答案",
            trace=[TraceStep(step_id=1, tool="hybrid_retrieve", elapsed_ms=1.0)],
            refs=[],
        )


def main() -> None:
    made = []

    def factory():
        tool = FakeRagTool()
        made.append(tool)
        return tool

    tool = RagDocSearchTool(tool_factory=factory)
    assert tool.name == "doc_search"
    assert made == [], "构造工具时不应初始化真实 RAG"

    first = tool.run({"question": "质保期是多久？"})
    second = tool.run({"question": "响应时限是多少？"})
    assert isinstance(first, ToolResult)
    assert first.data == "真实 RAG 答案"
    assert second.data == "真实 RAG 答案"
    assert len(made) == 1, "RAG 工具应只初始化一次"
    assert made[0].questions == ["质保期是多久？", "响应时限是多少？"]

    # 默认装配必须注册真实适配层，但此处不执行查询，故不会加载模型。
    live_orch = build_default_orchestrator(doc_search_backend="rag")
    assert isinstance(live_orch.registry.require("doc_search"), RagDocSearchTool)
    plan = route("维保合同的质保期是多少", live_orch.registry)
    assert [name for name, _ in plan.steps] == ["doc_search"]

    try:
        tool.run({"question": "  "})
    except ValueError:
        pass
    else:
        raise AssertionError("空问题必须被拒绝")

    print("ALL PASSED (真实 doc_search 适配层 / 懒加载 / ToolResult 透传)")


if __name__ == "__main__":
    main()
