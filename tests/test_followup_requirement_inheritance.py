"""S2-F/F3：接续轮的"缺对象"判定要看「本轮 ∪ 继承」。

## 为什么需要这组测试

用户给的原始场景（2026-10-16）：

    第 1 轮：公司有多少员工？
    第 2 轮：男的有多少？          ← 期望：接着员工问，答出男性员工数

修复前第 2 轮被追问「请补充要统计的业务对象（如客户、发票或员工）。」
—— 本轮清单里确实没有 object，但**上一轮已经说过是员工**。
`SemanticClarifier.decide` 此前只吃本轮清单，看不到继承。

## 这组测试守的两条边界

1. **该继承时要继承**：有合法继承对象时不追问，否则用户每问一个收窄条件
   都得重说一遍对象。
2. **不该顶包时不能顶包**：继承来的对象若本身 `unresolved=True`
   （上一轮是「它有多少？」），它替不了本轮的对象 —— 否则追问会被
   一个同样没落实的指代消掉，等于放过。

另外守一条**非接续轮**边界：不传 `inherited` 时行为必须与修复前完全一致，
否则新话题会被上一轮的上下文污染。
"""

from __future__ import annotations

import pytest

from app.clarifier import SemanticClarifier
from app.condition_archive import merge_condition_archive
from app.nl2sql.normalizer import normalize
from app.nl2sql.planning_models import QueryRequirements
from app.query_requirements import REQUIREMENTS_VERSION, extract_requirements


def _clean(question: str) -> QueryRequirements:
    """提取**本轮**清单，不带任何继承 —— 模拟"编排还没并入继承"的状态。"""
    return extract_requirements(question, normalize(question))


def _inherited_from(previous_question: str) -> QueryRequirements:
    """把上一轮问句走一遍档案合并，取出应当继承的要求。"""
    archive = merge_condition_archive(
        None,
        extract_requirements(previous_question, normalize(previous_question)),
        question=previous_question,
        is_followup=False,
    )
    return QueryRequirements(
        version=REQUIREMENTS_VERSION,
        original="",
        normalized_question="",
        requirements=archive.active_requirements,
    )


# ── 1. 该继承时要继承 ────────────────────────────────────────────────


def test_followup_value_filter_inherits_object_from_previous_turn():
    """用户原始场景：第 2 轮「男的有多少？」不该再问"要统计什么"。"""
    question = "男的有多少？"
    inherited = _inherited_from("公司有多少员工？")

    decision = SemanticClarifier().decide(question, normalize(question), _clean(question), inherited)

    assert decision.status == "continue", (
        "上一轮已说明是员工，第 2 轮不该再追问业务对象；"
        f"实际 reason_code={decision.reason_code}"
    )


def test_without_inheritance_the_same_question_still_asks():
    """非接续轮（不传继承）行为必须与修复前一致 —— 仍然追问。

    这条是上一条的对照：证明上一条的 `continue` 确实来自继承，
    而不是"缺对象检查整个失效了"。
    """
    question = "男的有多少？"

    decision = SemanticClarifier().decide(question, normalize(question), _clean(question), None)

    assert decision.status == "ask"
    assert decision.reason_code == "measure_without_object"


# ── 2. 未解析的继承对象不得顶包 ──────────────────────────────────────


def test_unresolved_inherited_object_cannot_stand_in():
    """上一轮是「它有多少？」时，那个没落实的"它"不能当本轮的锚。"""
    question = "男的有多少？"
    unresolved = _clean("它有多少？")

    decision = SemanticClarifier().decide(question, normalize(question), _clean(question), unresolved)

    assert decision.status == "ask", (
        "继承来的对象本身 `unresolved=True`，不能替本轮提供对象依据；"
        "否则「它」这类没有先行词的指代会被上一轮顶包、追问失效。"
    )
    assert decision.reason_code == "measure_without_object"


def test_empty_inheritance_behaves_like_no_inheritance():
    """空继承清单与 `None` 等价（`QueryRequirements()` 是合法的空值）。"""
    question = "男的有多少？"
    empty = QueryRequirements(version=REQUIREMENTS_VERSION, original="", normalized_question="")

    decision = SemanticClarifier().decide(question, normalize(question), _clean(question), empty)

    assert decision.status == "ask"
    assert decision.reason_code == "measure_without_object"


# ── 3. 继承只影响"缺对象"，不改其它判定 ──────────────────────────────


@pytest.mark.parametrize(
    "question",
    ["女性员工有多少？", "公司有多少员工？"],
)
def test_turn_that_already_has_object_is_unaffected_by_inheritance(question):
    """本轮自己说了对象时，有无继承结论必须一致。"""
    inherited = _inherited_from("公司有多少员工？")

    with_inherit = SemanticClarifier().decide(
        question, normalize(question), _clean(question), inherited
    )
    without_inherit = SemanticClarifier().decide(
        question, normalize(question), _clean(question), None
    )

    assert with_inherit.status == without_inherit.status == "continue"


def test_inheritance_does_not_suppress_document_scope_exemption():
    """文档问句仍走文档豁免，继承不改变这一点。

    「合同里怎么规定的？」没有业务对象是正常的，继承来一个对象
    也不该让它变成"要统计什么"的追问对象。
    """
    question = "合同里怎么规定的？"
    inherited = _inherited_from("公司有多少员工？")

    decision = SemanticClarifier().decide(question, normalize(question), _clean(question), inherited)

    assert decision.status == "continue", (
        "文档问句的对象不在业务表里，不该因为继承了业务对象就改变判定"
    )


def test_ranking_followup_without_object_inherits_too():
    """排序类同样受益：「那最多的呢？」接着上一轮的对象走。

    与 `measure_without_object` 是两条独立分支，都要并入继承判定，
    否则修了一半、排序类仍然会追问。
    """
    question = "那最多的呢？"
    inherited = _inherited_from("公司有多少员工？")

    decision = SemanticClarifier().decide(question, normalize(question), _clean(question), inherited)

    assert decision.reason_code != "ranking_without_object", (
        "继承了对象之后不该再报 ranking_without_object"
    )


# ── 4. 端到端：必须走编排真正的接线 ──────────────────────────────────
#
# ⚠ 上面几条都直接调 `SemanticClarifier().decide(...)`，因此只能证明"传对了
#    就判对"，**证明不了编排真的把继承传下去了**。变异验证实测：把
#    `app/orchestrator.py` 里的 `decide(question, norm, requirements, inherited)`
#    退回成不传 `inherited`，上面全部用例照样绿（M2 MISSED）。
#    所以这里补一条打在 `orchestrator.answer()` 上的断言 —— 这是唯一能
#    抓住接线退回的位置。


def _archive_after(orchestrator, session_id: str, question: str) -> None:
    """跑一轮并确认它真的答完了（档案只在答完后才写）。"""
    response = orchestrator.answer(question, session_id=session_id)
    assert response.need_clarify is False, (
        f"前置失败：「{question}」本身就不该澄清，否则档案不会写入"
    )


class _FakeNl2SqlTool:
    """端到端接线测试用的 NL2SQL 替身：返回固定行，不碰数据库、不调模型。

    这里**刻意不接真工具**：本组用例要证明的是"编排把继承传给了澄清器"，
    与 SQL 生成质量无关。接真链路会让用例依赖数据库、模型与网络，
    既慢又脆。
    """

    name = "nl2sql"

    def run(self, payload):  # noqa: ANN001, ANN201
        from app.contracts import ToolResult, TraceStep

        return ToolResult(
            data={
                "rows": [{"customer_count": 59}],
                "sql": "SELECT COUNT(*) AS customer_count FROM customer LIMIT 1000",
                "columns": ["customer_count"],
            },
            # ⚠ 这个 trace 步骤**不能省**：`Orchestrator._execution_succeeded`
            #   （app/orchestrator.py:884）要求存在一个工具名不在
            #   {normalizer, semantic_clarifier} 里的步骤，才认定"这轮真的执行过"。
            #   没有它，条件档案不会被写下来，本组用例的前置就塌了。
            trace=[
                TraceStep(
                    step_id=1,
                    tool="nl2sql",
                    input={"question": (payload or {}).get("question", "")},
                    output={"sql": "SELECT COUNT(*) AS customer_count FROM customer LIMIT 1000"},
                    elapsed_ms=1.0,
                    hit=None,
                )
            ],
            refs=[],
        )


class _WiringRegistry:
    """只提供 `nl2sql` 一个真工具的注册表，其余名字一律 `has()=False`。"""

    def __init__(self) -> None:
        self._tool = _FakeNl2SqlTool()

    def has(self, name: str) -> bool:
        return name == self._tool.name

    def names(self) -> list[str]:
        return [self._tool.name]

    def get(self, name: str):
        if name != self._tool.name:
            raise AssertionError(f"未登记的工具：{name}")
        return self._tool


def test_orchestrator_followup_keeps_the_object_so_no_spurious_question():
    """端到端：接续轮不得因为"本轮没提对象"而追问。

    ## 实测到的真实机制（与最初设计假设不同，必须如实记录）

    最初以为接续轮的 object 靠 `_augment_from_inherited` 从条件档案补进来
    （标 `source="session"`）。**实测不是**：

        turn1 prepared = '平台上一共有多少位客户？'
        turn2 prepared = '平台上一共有多少位客户？\\n用户补充或修改：那最多的呢？'
                         ↑ `SessionMemory.prepare_turn` 把上一轮原话拼进来了

    于是 `extract_requirements` 从**拼接文本**里就把「客户」当成 `literal` 认出来，
    `_augment_from_inherited` 因为"该 kind 已存在"而跳过 —— 实测 C（无继承）
    与 D（有继承）产出的清单**逐条相同**。

    所以本条只断言**可观察结果**：接续轮不追问。不把断言绑在 `source="session"`
    上，因为真实路径下根本不会出现那个标记。档案继承那条路是给**不拼接**的
    调用方（直接调 `decide()`）准备的，由上面几条单元用例覆盖。
    """
    from app.orchestrator import Orchestrator

    session_id = "s2f-f3-merge"
    orchestrator = Orchestrator(registry=_WiringRegistry())

    _archive_after(orchestrator, session_id, "平台上一共有多少位客户？")

    archive = orchestrator.session_memory.condition_archive(session_id)
    assert archive is not None, "第 1 轮应当写下条件档案"
    assert any(
        item.kind == "object" for item in archive.active_requirements
    ), f"档案里应当有对象条件，实际：{[i.kind for i in archive.active_requirements]}"

    # 第 2 轮：短接续问句，本身不含对象。
    response = orchestrator.answer("那最多的呢？", session_id=session_id)

    clarification_steps = [
        step for step in response.trace if step.tool == "semantic_clarifier"
    ]
    assert clarification_steps, "澄清步没有出现在 trace 里"
    output = clarification_steps[0].output or {}
    listed = output.get("requirements") or []

    objects = [item for item in listed if item.get("kind") == "object"]
    assert objects, (
        "接续轮的清单里必须仍有 object，否则会退化成"
        "『请补充要统计的业务对象』。"
        f"实际清单：{[(i.get('kind'), i.get('surface'), i.get('source')) for i in listed]}"
    )
    assert (output.get("reason_code") or "") != "ranking_without_object", (
        "有对象依据时不该报 ranking_without_object"
    )


def test_orchestrator_does_not_inherit_on_a_fresh_topic():
    """反向：非接续轮不得继承（新话题不该被上一轮污染）。"""
    from app.orchestrator import Orchestrator

    session_id = "s2f-f3-fresh"
    orchestrator = Orchestrator(registry=_WiringRegistry())

    _archive_after(orchestrator, session_id, "平台上一共有多少位客户？")

    # 一个明显的新话题、且长到不会被判成接续（`_is_followup` 对 >24 字直接 False）
    fresh = "智能计算设备年度维保服务合同里，质保期是多久？这个问题请单独回答"
    response = orchestrator.answer(fresh, session_id=session_id)

    clarification_steps = [
        step for step in response.trace if step.tool == "semantic_clarifier"
    ]
    assert clarification_steps, "澄清步没有出现在 trace 里"
    output = clarification_steps[0].output or {}

    assert (output.get("inherited") or {}).get("applied") is False, (
        "新话题不该继承上一轮条件；若这里是 True，说明接续判定过宽"
    )
    listed = output.get("requirements") or []
    assert not [item for item in listed if item.get("source") == "session"], (
        f"新话题的清单里不该有继承条，实际：{listed}"
    )
