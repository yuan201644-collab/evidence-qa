"""S1 集成测试：澄清决策接进编排器之后的行为边界。

`tests/test_query_requirements.py` 只测提取器本身（纯函数）。本文件补的是
**两条集成路径** —— 用户复核时指出这两个 bug 之所以活下来，正是因为当时
只测了提取器、没测它与澄清器/编排器的接线：

  1. **分支顺序**：`app/clarifier.py` 里未解析指代检查必须排在
     `document_scope` / `cross_source_object` 豁免**之前**。排在后面时
     「这份合同怎么规定？」会被文档豁免直接放行，提取器认出的
     `unresolved=True` 白认了。
  2. **提取用原文**：`app/orchestrator.py` 必须把**用户原话**交给提取器，
     而 `question` 此时已被 normalizer 改写。用改写文本会让清单记下
     用户从没说过的词（「买家数量是多少？」→ `surface='customer'`），
     而 `span` 还指向原话位置，留痕与事实对不上。

  3. **澄清时零工具调用**：这是能拦住第 1 条的那道断言 —— 追问意味着
     "在拿到对象之前不该执行查询"。若澄清返回 `ask` 后仍有工具被调用，
     说明系统在信息不足时就动了手。
"""

from __future__ import annotations

import pytest

from app.clarifier import SemanticClarifier
from app.query_requirements import extract_requirements


class _NoopTool:
    """执行路径上的占位工具：不改状态、不碰数据库、不调模型。"""

    name = "__no_tool__"

    def run(self, payload):  # noqa: ANN001, ANN201 - 形状跟随 ToolRegistry 协议
        from app.contracts import ToolResult

        return ToolResult(data={"note": "测试占位工具"}, trace=[], refs=[])


class _CountingRegistry:
    """记录任何被查询过的工具名，并让路由能走完但不真的执行工具。

    这组用例里有两种路径：

    * **澄清路径**（`test_clarification_path_calls_no_tools`）——应当
      一次都不碰注册表，`asked` 必须为空。
    * **执行路径**（原话核对那两条）——`买家数量是多少？` 这类问句会
      真的走到 `route()` 与 `_execute()`，因此注册表必须能交出工具。

    两者靠 `asked` 里是否出现 `<names>` / `<get:…>` 区分。
    """

    #: 回落分支 `app/orchestrator.py:277` 取 `names()[0]`。
    _FALLBACK = "__no_tool__"

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._tool = _NoopTool()

    def has(self, name: str) -> bool:
        self.asked.append(name)
        return False

    def names(self) -> list[str]:
        self.asked.append("<names>")
        return [self._FALLBACK]

    def get(self, name: str):
        self.asked.append(f"<get:{name}>")
        if name != self._FALLBACK:
            raise AssertionError(f"不应取用未登记的工具：{name}")
        return self._tool


# ── 集成路径 1：分支顺序 ──────────────────────────────────────────────
#
# 这一组是 CS14 误伤修复的反向保证：指代检查必须**先于**文档豁免。


@pytest.mark.parametrize(
    "question,surface",
    [
        ("这份合同怎么规定？", "这份"),
        ("该合同的质保期是多少？", "该"),
        ("上述协议里写了什么？", "上述"),
        ("此份文档的生效日期是哪天？", "此份"),
    ],
)
def test_document_scope_does_not_bypass_unresolved_deixis(question, surface):
    """文档问句里的指示词没有先行词时，必须追问而不是直接进检索。

    这些句子**同时**含 `document_scope`（合同/协议/文档）与未解析指代。
    只要豁免分支排在前面，`document_scope` 就会把 `unresolved` 掩盖掉。
    """
    requirements = extract_requirements(question)
    unresolved = [item.surface for item in requirements.requirements if item.unresolved]
    assert surface in unresolved, "前提：提取器应认出未解析指代"

    decision = SemanticClarifier().decide(question, None, requirements)

    assert decision.status == "ask", (
        f"「{question}」的「{surface}」没有先行词，应追问；"
        f"若这里返回 continue，说明文档豁免又排到了指代检查前面。"
    )
    assert decision.reason_code == "unresolved_reference"


def test_anchored_deixis_in_document_question_is_not_asked():
    """反面：指代被编号锚定时不该追问（否则就是把 CS14 那类误伤搬回来）。"""
    question = "请核验该工单的合同服务恢复时限，并给出条款依据。 工单编号 WO-QF-001。"
    requirements = extract_requirements(question)
    decision = SemanticClarifier().decide(question, None, requirements)
    assert decision.status == "continue"


# ── 集成路径 2：清单必须来自原话 ──────────────────────────────────────


@pytest.mark.parametrize(
    "spoken,rewritten",
    [
        ("买家数量是多少？", "customer数量是多少？"),
        ("顾客有多少位？", "customer有多少位？"),
        ("雇员有几个？", "employee有几个？"),
    ],
)
def test_requirements_use_original_not_rewritten_text(spoken, rewritten):
    """别名被改写后，清单不能记下用户没说过、且 span 对不上的词。

    ⚠ 这条断言打在**编排真正调用的那条线**上：直接跑 `answer()`，从 trace 里
    读澄清步实际收到的清单，而不是自己再调一次静态方法。自己传参只能证明
    "传对了就抽对"，证明不了编排传的是哪个文本 —— 早先的写法正是这样漏掉
    了一次接线回归（由变异测试发现）。

    这里用一句会走到澄清的短问句，避免触碰数据库与大模型。
    """
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(registry=_CountingRegistry())
    response = orchestrator.answer(spoken, session_id=f"s1-orig-{abs(hash(spoken))}")

    steps = [step for step in response.trace if step.tool == "semantic_clarifier"]
    assert steps, "澄清步没有出现在 trace 里，无法核对清单来源"
    listed = (steps[0].output or {}).get("requirements") or []

    for item in listed:
        surface = item.get("surface", "")
        if not surface:
            continue
        assert surface in spoken, (
            f"清单里的「{surface}」不在用户原话「{spoken}」中 —— "
            f"编排可能把改写后的「{rewritten}」喂给了提取器"
        )


def test_orchestrator_feeds_original_text_to_extractor():
    """硬断言：编排喂给提取器的是**用户本轮原话**，不是改写文本。

    改写成 `customer数量是多少？` 之后，清单里若出现 `customer` 这种
    用户没写过的英文表名，就说明喂进去的是改写文本。
    """
    from app.orchestrator import Orchestrator

    spoken = "买家数量是多少？"
    orchestrator = Orchestrator(registry=_CountingRegistry())
    response = orchestrator.answer(spoken, session_id="s1-feed-original")

    steps = [step for step in response.trace if step.tool == "semantic_clarifier"]
    assert steps, "澄清步没有出现在 trace 里，无法核对清单来源"
    surfaces = {
        item.get("surface", "")
        for item in ((steps[0].output or {}).get("requirements") or [])
    }
    assert not (surfaces & {"customer", "invoice", "employee"}), (
        f"清单里出现了改写文本才有的英文表名：{surfaces}"
    )


# ── 集成路径 3：澄清时零工具调用 ──────────────────────────────────────


def test_clarification_path_calls_no_tools():
    """追问时不得执行任何查询工具。

    这是拦住"分支顺序写反"的那道断言：若系统在信息不足时仍然动了手，
    说明澄清决策没能在路由之前短路。
    """
    from app.orchestrator import Orchestrator

    registry = _CountingRegistry()
    orchestrator = Orchestrator(registry=registry)

    response = orchestrator.answer("有多少位？", session_id="s1-integration")

    assert response.need_clarify is True
    assert registry.asked == [], (
        f"澄清阶段不应触碰工具注册表，实际查询了：{registry.asked}"
    )
    tools_in_trace = {step.tool for step in response.trace}
    assert tools_in_trace <= {"normalizer", "semantic_clarifier"}, (
        f"澄清路径只应有识别与澄清两步，实际 trace 含：{sorted(tools_in_trace)}"
    )
