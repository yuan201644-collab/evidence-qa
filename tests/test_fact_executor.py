"""事实计划执行器的依赖早停与证据闸门测试。"""

import unittest

from app.contracts import DocRef, SqlRef, ToolResult, TraceStep
from app.evidence_ledger import FactPlan, FactTask
from app.fact_executor import FactDecision, FactPlanExecutor, FactTaskOutput, tool_task_handler


class FactPlanExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = FactPlan()
        self.plan.add_task(FactTask("identity", "明确的对象关系"))
        self.plan.add_task(FactTask("clause", "文档条款", depends_on=("identity",)))
        self.plan.add_task(FactTask("verdict", "最终判定", depends_on=("clause",)))

    @staticmethod
    def sql_result() -> ToolResult:
        ref = SqlRef(
            kind="sql",
            table="demo.mapping",
            column="contract_id",
            row_pk="DEV-1|C-1",
            snippet="DEV-1 明确映射到 C-1",
        )
        step = TraceStep(
            step_id=1,
            tool="nl2sql",
            input={"question": "查询设备映射"},
            output={"row_count": 1},
            elapsed_ms=1.0,
            hit=[ref],
        )
        return ToolResult(data={"rows": [{"contract_id": "C-1"}]}, trace=[step], refs=[ref])

    @staticmethod
    def doc_result() -> ToolResult:
        ref = DocRef(kind="doc", doc_id="C-1", snippet="修复应在 8 小时内完成。")
        step = TraceStep(
            step_id=1,
            tool="doc_search",
            input={"question": "C-1 修复时限"},
            output={"status": "retrieved"},
            elapsed_ms=1.0,
            hit=[ref],
        )
        return ToolResult(data="8 小时", trace=[step], refs=[ref])

    def test_real_tool_protocol_handlers_run_in_order_and_collect_trace(self) -> None:
        calls: list[str] = []
        sql_result = self.sql_result()
        doc_result = self.doc_result()

        def identity(_context, _facts):
            calls.append("identity")
            ref = sql_result.refs[0]
            return FactTaskOutput(FactDecision("DEV-1→C-1", "supported", (ref,)), sql_result)

        def clause(_context, facts):
            calls.append("clause")
            self.assertEqual(facts["identity"].state, "supported")
            ref = doc_result.refs[0]
            return FactTaskOutput(FactDecision("8 小时", "supported", (ref,)), doc_result)

        def verdict(_context, facts):
            calls.append("verdict")
            self.assertEqual(facts["clause"].value, "8 小时")
            evidence = (sql_result.refs[0], doc_result.refs[0])
            return FactTaskOutput(FactDecision("可进入确定性比较", "supported", evidence))

        run = FactPlanExecutor({
            "identity": identity,
            "clause": clause,
            "verdict": verdict,
        }).run(self.plan)

        self.assertEqual(calls, ["identity", "clause", "verdict"])
        self.assertEqual(run.records["verdict"].state, "supported")
        self.assertEqual([step.tool for step in run.trace], ["nl2sql", "doc_search"])
        self.assertEqual(len(run.refs), 2)

    def test_unsupported_fact_stops_dependents(self) -> None:
        calls: list[str] = []

        def identity(_context, _facts):
            calls.append("identity")
            return FactTaskOutput(FactDecision("存在候选映射", "candidate"), self.sql_result())

        def should_not_run(_context, _facts):
            calls.append("clause")
            return FactTaskOutput(FactDecision("8 小时", "supported", (self.doc_result().refs[0],)), self.doc_result())

        run = FactPlanExecutor({"identity": identity, "clause": should_not_run}).run(self.plan)

        self.assertEqual(calls, ["identity"])
        self.assertEqual(run.records["clause"].state, "insufficient")
        self.assertEqual(run.records["verdict"].state, "insufficient")
        self.assertIn("clause", run.skipped)

    def test_supported_early_stop_reason_is_exposed_as_deterministic_alignment_trace(self) -> None:
        sql_result = self.sql_result()

        def identity(_context, _facts):
            return FactTaskOutput(FactDecision(
                "客户范围冲突，停止合同判定。", "insufficient",
                tuple(sql_result.refs), "customer_scope_conflict",
            ), sql_result)

        run = FactPlanExecutor({"identity": identity}, verification_mode="strict").run(self.plan)

        self.assertEqual(run.verification_mode, "strict")
        self.assertEqual(run.stop_reason_code, "customer_scope_conflict")
        self.assertEqual(run.records["clause"].state, "insufficient")
        self.assertEqual(run.trace[-1].tool, "cross_source_alignment")
        self.assertEqual(run.trace[-1].output, {
            "status": "not_determined",
            "stop_reason_code": "customer_scope_conflict",
        })
        self.assertEqual(run.trace[-1].hit, sql_result.refs)

    def test_unsupported_event_fact_prevents_dependent_document_tool_call(self) -> None:
        plan = FactPlan()
        plan.add_task(FactTask("mapping", "映射", source_candidates=("nl2sql",)))
        plan.add_task(FactTask("events", "必要事件", depends_on=("mapping",)))
        plan.add_task(FactTask(
            "document", "合同条款", depends_on=("mapping", "events"),
        ))
        sql_result = self.sql_result()
        doc_calls: list[str] = []

        def mapping(_context, _facts):
            return FactTaskOutput(FactDecision("受控映射", "supported", tuple(sql_result.refs)), sql_result)

        def events(_context, facts):
            return FactTaskOutput(FactDecision(
                "事件时间倒序", "contradicted", tuple(facts["mapping"].evidence_refs),
                "event_time_reversed",
            ))

        def document(_context, _facts):
            doc_calls.append("called")
            doc_result = self.doc_result()
            return FactTaskOutput(FactDecision("8 小时", "supported", tuple(doc_result.refs)), doc_result)

        run = FactPlanExecutor({
            "mapping": mapping, "events": events, "document": document,
        }).run(plan)

        self.assertEqual(doc_calls, [])
        self.assertEqual(run.records["document"].state, "insufficient")
        self.assertEqual(run.stop_reason_code, "event_time_reversed")
        self.assertEqual(run.trace[-1].tool, "cross_source_alignment")
        self.assertEqual(run.trace[-1].hit, sql_result.refs)

    def test_unsupported_early_stop_reason_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "白名单"):
            FactDecision("停止", "insufficient", (), "made_up_reason")
        with self.assertRaisesRegex(ValueError, "受支持事实"):
            FactDecision("支持", "supported", (), "customer_scope_conflict")

    def test_ablation_can_attempt_dependents_without_promoting_unsupported_chain(self) -> None:
        calls: list[str] = []
        sql_result = self.sql_result()
        doc_result = self.doc_result()

        def identity(_context, _facts):
            calls.append("identity")
            return FactTaskOutput(FactDecision("候选但未确认的映射", "insufficient"), sql_result)

        def clause(_context, facts):
            calls.append("clause")
            self.assertEqual(facts["identity"].state, "insufficient")
            return FactTaskOutput(
                FactDecision("候选条款", "supported", (doc_result.refs[0],)), doc_result
            )

        run = FactPlanExecutor(
            {"identity": identity, "clause": clause},
            stop_on_unsupported_dependencies=False,
            verification_mode="ablation",
        ).run(self.plan)

        self.assertEqual(run.verification_mode, "ablation")
        self.assertEqual(calls, ["identity", "clause"])
        self.assertEqual(run.records["clause"].state, "candidate")
        self.assertNotIn("clause", run.skipped)

    def test_supported_decision_without_registered_evidence_is_downgraded(self) -> None:
        def identity(_context, _facts):
            invented_ref = SqlRef(
                kind="sql", table="demo.mapping", column="id", row_pk="invented", snippet="未执行查询"
            )
            return FactTaskOutput(FactDecision("DEV-1→C-1", "supported", (invented_ref,)))

        run = FactPlanExecutor({"identity": identity}).run(self.plan)

        self.assertEqual(run.records["identity"].state, "insufficient")
        self.assertIn("本轮可核验", run.skipped["identity"])
        self.assertEqual(run.records["clause"].state, "insufficient")

    def test_handler_failure_is_recorded_as_structured_trace(self) -> None:
        class QueryFailure(Exception):
            sqlstate = "42704"

        def fail(_context, _facts):
            raise QueryFailure("database detail is deliberately not copied to trace")

        run = FactPlanExecutor({"identity": fail}).run(self.plan)

        failure = next(step for step in run.trace if step.tool == "fact_task_failure")
        self.assertEqual(failure.tool, "fact_task_failure")
        self.assertEqual(failure.input["task_id"], "identity")
        self.assertEqual(failure.output, {
            "error_type": "QueryFailure",
            "sqlstate": "42704",
            "status": "failed",
            "diagnostic_code": "task_execution_failed",
        })
        self.assertGreaterEqual(failure.elapsed_ms, 0)
        self.assertNotIn("database detail", str(failure.output))
        not_run = next(step for step in run.trace if step.tool == "fact_task_not_run")
        self.assertEqual(not_run.output["task_id"], "clause")
        self.assertEqual(not_run.output["reason_code"], "unsupported_dependencies")

    def test_not_run_output_cannot_claim_tool_result_or_supported_decision(self) -> None:
        sql_result = self.sql_result()
        invalid_outputs = (
            lambda: FactTaskOutput(
                FactDecision("未执行", "insufficient"), sql_result,
                not_run_reason_code="handler_not_configured",
            ),
            lambda: FactTaskOutput(
                FactDecision("已支持", "supported", tuple(sql_result.refs)),
                not_run_reason_code="handler_not_configured",
            ),
        )
        for make_output in invalid_outputs:
            def invalid_handler(_context, _facts):
                return make_output()

            run = FactPlanExecutor({"identity": invalid_handler}).run(self.plan)
            failure = next(step for step in run.trace if step.tool == "fact_task_failure")
            self.assertEqual(failure.output, {
                "error_type": "FactTaskOutputInvariantError",
                "sqlstate": None,
                "status": "failed",
                "diagnostic_code": "not_run_output_conflict",
            })
            self.assertEqual(run.records["identity"].state, "insufficient")
            self.assertEqual(run.refs, [])
            self.assertNotIn("nl2sql", [step.tool for step in run.trace])

    def test_merged_trace_step_ids_are_unique_and_sequential(self) -> None:
        sql_result = self.sql_result()
        doc_result = self.doc_result()

        def identity(_context, _facts):
            return FactTaskOutput(FactDecision("DEV-1→C-1", "supported", tuple(sql_result.refs)), sql_result)

        def clause(_context, _facts):
            return FactTaskOutput(FactDecision("8 小时", "supported", tuple(doc_result.refs)), doc_result)

        run = FactPlanExecutor({"identity": identity, "clause": clause}).run(self.plan)

        self.assertEqual([step.step_id for step in run.trace], list(range(1, len(run.trace) + 1)))

    def test_tool_adapter_calls_existing_tool_protocol_and_explicit_validator(self) -> None:
        class Tool:
            def __init__(self):
                self.payloads = []

            def run(self, payload):
                self.payloads.append(payload)
                return FactPlanExecutorTests.sql_result()

        tool = Tool()
        ref = self.sql_result().refs[0]
        handler = tool_task_handler(
            tool,
            lambda context, facts: {"question": context, "hint": facts.get("identity")},
            lambda result, _context, _facts: FactDecision(
                result.data["rows"][0]["contract_id"], "supported", (ref,)
            ),
        )

        run = FactPlanExecutor({"identity": handler}).run(self.plan, "查询 DEV-1 合同")

        self.assertEqual(tool.payloads, [{"question": "查询 DEV-1 合同", "hint": None}])
        self.assertEqual(run.records["identity"].value, "C-1")
        self.assertEqual(run.records["identity"].state, "supported")

    def test_deterministic_row_references_are_attached_to_executed_sql_trace(self) -> None:
        class Tool:
            def run(self, _payload):
                result = FactPlanExecutorTests.sql_result()
                return result.model_copy(update={
                    "trace": [result.trace[0].model_copy(update={
                        "output": {"sql": "SELECT work_order_id FROM work_orders", "row_count": 1}
                    })]
                })

        derived = SqlRef(
            kind="sql", table="contracts", column="contract_id", row_pk="C-1", snippet="合同 C-1"
        )
        handler = tool_task_handler(
            Tool(),
            lambda _context, _facts: {"question": "查询"},
            lambda _result, _context, _facts: FactDecision("C-1", "supported", (derived,)),
        )

        run = FactPlanExecutor({"identity": handler}).run(self.plan)

        self.assertEqual(run.records["identity"].state, "supported")
        self.assertIn(derived, run.trace[0].hit)
        self.assertIn(derived, run.refs)


if __name__ == "__main__":
    unittest.main()
