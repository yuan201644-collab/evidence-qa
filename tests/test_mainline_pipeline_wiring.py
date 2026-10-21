from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import app.orchestrator as orchestrator_module
from app.contracts import ToolResult, TraceStep
from app.nl2sql.normalizer import NormalizeResult
from app.nl2sql.planning_models import QueryIntent
from app.nl2sql.pipeline import Nl2SqlPipeline
from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta
from app.orchestrator import Orchestrator, RoutePlan


class _Client:
    def chat(self, system, user):
        return "SELECT customer_id FROM customer"


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, sql):
        self.calls.append(sql)
        return SimpleNamespace(
            rows=[{"customer_id": 1}], columns=["customer_id"], row_count=1,
            error=None, sqlstate=None,
        )


class _MustNotRun:
    def link(self, *_args, **_kwargs):
        raise AssertionError("关闭语义规划时不得调用定位器")


def _catalog():
    customer = TableMeta(
        name="customer",
        schema="public",
        primary_key=("customer_id",),
        columns=(ColumnMeta("customer_id", "integer", nullable=False, is_primary_key=True),),
    )
    return SchemaCatalog([customer])


def test_legacy_path_runs_without_calling_semantic_planning_components() -> None:
    executor = _Executor()
    pipeline = Nl2SqlPipeline(
        _Client(), catalog=_catalog(), executor=executor,
        auto_limit=0, linker=_MustNotRun(), semantic_planning_enabled=False,
        # 关掉要求覆盖闸门：这条用例只关心"老路径不碰语义规划组件"。
        requirement_gate="off",
    )

    outcome = pipeline.run("查询客户编号")

    assert outcome.error is None, outcome.error
    assert executor.calls == ["SELECT customer_id FROM customer"]
    assert [step.tool for step in outcome.trace] == ["nl2sql", "nl2sql", "nl2sql"]


def test_opt_in_path_records_planning_and_semantic_steps() -> None:
    executor = _Executor()
    pipeline = Nl2SqlPipeline(
        _Client(), catalog=_catalog(), executor=executor,
        auto_limit=0, semantic_planning_enabled=True,
    )

    outcome = pipeline.run("查询 customer 表")

    assert outcome.error is None, outcome.error
    assert executor.calls == ["SELECT customer_id FROM customer"]
    tools = [step.tool for step in outcome.trace]
    assert tools[:3] == ["schema_linker", "join_planner", "nl2sql"]
    assert "sql_semantic_validator" in tools


def test_orchestrator_preserves_original_question_for_schema_linking() -> None:
    class CaptureTool:
        name = "nl2sql"

        def run(self, payload):
            self.payload = payload
            return ToolResult(data={})

    class CaptureRegistry:
        def __init__(self, tool):
            self.tool = tool

        def get(self, _name):
            return self.tool

    tool = CaptureTool()
    orchestrator = Orchestrator(CaptureRegistry(tool))
    original_question = "按销售原因类型统计关联的不同订单数。"
    plan = RoutePlan(
        steps=[("nl2sql", "")],
        reason="test",
        query_intent=QueryIntent(
            objects=("ReasonType", "SalesOrderID"),
            source_question=original_question,
        ),
    )

    orchestrator._execute("按ReasonType统计SalesOrderID。", plan)

    assert tool.payload["query_intent"]["source_question"] == original_question


def test_growth_clarification_waits_for_comparison_period_before_routing(monkeypatch) -> None:
    class CaptureTool:
        name = "nl2sql"

        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            return ToolResult(
                data={"conclusion": "销售额增长率已查询。"},
                trace=[TraceStep(
                    step_id=1,
                    tool="nl2sql",
                    input={"question": payload["question"]},
                    output={"row_count": 1},
                    elapsed_ms=0,
                )],
            )

    class CaptureRegistry:
        def __init__(self, tool):
            self.tool = tool

        def get(self, _name):
            return self.tool

    tool = CaptureTool()
    orchestrator = Orchestrator(CaptureRegistry(tool))
    routed_questions = []

    def capture_route(question, _registry):
        routed_questions.append(question)
        return RoutePlan(steps=[("nl2sql", "")], reason="clarification integration test")

    monkeypatch.setattr(orchestrator_module, "route", capture_route)

    first = orchestrator.answer("2023 年销售额增长率是多少？", "growth-session")

    assert first.need_clarify is True
    assert [step.tool for step in first.trace] == ["normalizer", "semantic_clarifier"]
    assert routed_questions == []
    assert tool.payloads == []

    resumed = orchestrator.answer("2023 年相比 2022 年", "growth-session")

    assert resumed.need_clarify is False
    assert resumed.answer == "销售额增长率已查询。"
    assert len(routed_questions) == 1
    assert "2023 年销售额增长率是多少？" in routed_questions[0]
    assert "2023 年相比 2022 年" in routed_questions[0]
    assert len(tool.payloads) == 1


def test_unanchored_person_followup_is_clarified_before_tool_execution(monkeypatch) -> None:
    class CaptureTool:
        name = "nl2sql"

        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            return ToolResult(data={"conclusion": "客户共 59 位。"})

    class CaptureRegistry:
        def __init__(self, tool):
            self.tool = tool

        def get(self, _name):
            return self.tool

    tool = CaptureTool()
    orchestrator = Orchestrator(CaptureRegistry(tool))
    routed_questions = []

    def capture_route(question, _registry):
        routed_questions.append(question)
        return RoutePlan(steps=[("nl2sql", "")], reason="pronoun clarification integration test")

    monkeypatch.setattr(orchestrator_module, "route", capture_route)

    first = orchestrator.answer("平台上一共有多少位客户？", "pronoun-session")
    followup = orchestrator.answer("他总共消费了多少？", "pronoun-session")

    assert first.need_clarify is False
    assert followup.need_clarify is True
    assert "姓名/编号" in followup.answer
    assert followup.sql is None
    assert [step.tool for step in followup.trace] == ["normalizer", "semantic_clarifier"]
    assert len(routed_questions) == 1
    assert len(tool.payloads) == 1


def test_orchestrator_serializes_same_session_but_not_other_sessions(monkeypatch) -> None:
    slow_started = Event()
    release_slow = Event()
    same_session_second_started = Event()
    other_session_started = Event()
    active = 0
    max_active = 0
    active_lock = Lock()

    class ConcurrentTool:
        name = "nl2sql"

        def run(self, payload):
            nonlocal active, max_active
            question = payload["question"]
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                if "slow" in question:
                    slow_started.set()
                    assert release_slow.wait(2)
                elif "same-fast" in question:
                    same_session_second_started.set()
                elif "other-session" in question:
                    other_session_started.set()
                return ToolResult(data={"conclusion": "done"})
            finally:
                with active_lock:
                    active -= 1

    class Registry:
        def get(self, _name):
            return tool

    tool = ConcurrentTool()
    orchestrator = Orchestrator(Registry())
    monkeypatch.setattr(
        orchestrator_module,
        "normalize",
        lambda question, **_kwargs: NormalizeResult(
            original=question,
            normalized=question,
        ),
    )
    monkeypatch.setattr(
        orchestrator_module,
        "route",
        lambda _question, _registry: RoutePlan(
            steps=[("nl2sql", "")],
            reason="concurrency test",
        ),
    )

    with ThreadPoolExecutor(max_workers=3) as pool:
        slow = pool.submit(orchestrator.answer, "查询客户 slow", "session-a")
        assert slow_started.wait(1)
        same_session = pool.submit(
            orchestrator.answer, "查询客户 same-fast", "session-a"
        )
        other_session = pool.submit(
            orchestrator.answer, "查询客户 other-session", "session-b"
        )

        assert other_session_started.wait(1)
        assert not same_session_second_started.wait(0.05)
        release_slow.set()
        assert slow.result(timeout=2).answer == "done。"
        assert same_session.result(timeout=2).answer == "done。"
        assert other_session.result(timeout=2).answer == "done。"

    assert same_session_second_started.is_set()
    assert max_active == 2  # The other session proceeded while session-a was held.
    assert orchestrator.session_memory._request_locks == {}
