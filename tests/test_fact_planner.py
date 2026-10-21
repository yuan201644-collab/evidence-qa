import unittest
from unittest.mock import patch

from app.evidence_ledger import FactPlan, FactTask
from app.fact_planner import (
    build_work_order_sla_plan,
    requests_first_response,
    requests_repair_completion,
)
from app.orchestrator import Orchestrator, RoutePlan
from app.tools.registry import ToolRegistry


class WorkOrderSlaPlanTests(unittest.TestCase):
    def test_plan_encodes_mapping_and_evidence_dependencies(self) -> None:
        plan = build_work_order_sla_plan("wo-2026-0001")

        self.assertEqual(plan.tasks["work_order_facts"].target_fact, "工单 WO-2026-0001 的设备与必要事件")
        self.assertEqual(plan.tasks["controlled_contract_mapping"].depends_on, ("work_order_facts",))
        self.assertEqual(
            plan.tasks["contract_repair_limit"].depends_on,
            ("controlled_contract_mapping", "repair_duration"),
        )
        self.assertEqual(
            plan.tasks["repair_duration"].depends_on,
            ("work_order_facts", "controlled_contract_mapping"),
        )
        self.assertEqual(
            plan.tasks["sla_verdict"].depends_on,
            ("contract_repair_limit", "repair_duration"),
        )
        self.assertEqual(plan.validate()[-1], "sla_verdict")

    def test_plan_rejects_unrecognized_work_order_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "WO-区域或年份-序号"):
            build_work_order_sla_plan("DEV-001")

    def test_plan_accepts_holdout_municipal_work_order_identifier(self) -> None:
        plan = build_work_order_sla_plan("wo-nj-911")
        self.assertEqual(
            plan.tasks["work_order_facts"].target_fact,
            "工单 WO-NJ-911 的设备与必要事件",
        )

    def test_plan_adds_first_response_branch_only_when_question_requests_it(self) -> None:
        plan = build_work_order_sla_plan("WO-2026-0002", include_first_response=True)

        self.assertEqual(
            plan.tasks["first_response_duration"].depends_on,
            ("repair_duration",),
        )
        self.assertEqual(
            plan.tasks["first_response_verdict"].depends_on,
            ("contract_repair_limit", "first_response_duration"),
        )
        self.assertNotIn("first_response_verdict", plan.tasks["sla_verdict"].depends_on)

    def test_negated_metric_mentions_do_not_add_optional_facts(self) -> None:
        self.assertFalse(requests_first_response("请判断修复是否达标，不需要核验首次响应。"))
        self.assertFalse(requests_first_response("不讨论首次响应，只判断修复是否达标。"))
        self.assertFalse(requests_first_response("无需统计首次响应，只判断修复是否达标。"))
        self.assertFalse(requests_repair_completion("不需要判断修复，请只回答首次响应。"))
        self.assertTrue(requests_first_response("同时核对首次响应和修复是否达标。"))
        self.assertTrue(requests_repair_completion("请核对首次响应和修复是否达标。"))

    def test_orchestrator_stops_before_tools_for_invalid_fact_plan(self) -> None:
        invalid_plan = FactPlan()
        invalid_plan.add_task(FactTask("a", "事实 A", depends_on=("b",)))
        invalid_plan.add_task(FactTask("b", "事实 B", depends_on=("a",)))
        orchestrator = Orchestrator(ToolRegistry())
        route_plan = RoutePlan(
            steps=[("cross_source_demo", "WO-2026-0001")],
            reason="test invalid plan",
            fact_plan=invalid_plan,
        )

        with patch("app.orchestrator.route", return_value=route_plan), patch.object(
            orchestrator, "_execute"
        ) as execute:
            with self.assertRaisesRegex(ValueError, "循环"):
                orchestrator.answer("工单 WO-2026-0001 的时限？")

        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
