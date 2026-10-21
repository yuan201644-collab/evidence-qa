"""检查演示开关兼容性；默认工具用模拟实现，只证明路由与契约不变。"""
import os
import unittest
from unittest.mock import patch

from app.orchestrator import build_default_orchestrator, route


class RoutingTests(unittest.TestCase):
    def test_unset_and_disabled_switch_have_same_original_routes(self) -> None:
        with patch.dict(os.environ, {"DOC_SEARCH_BACKEND": "mock", "GRAPH_BACKEND": "mock"}):
            os.environ.pop("CROSS_SOURCE_DEMO_ENABLED", None)
            default = build_default_orchestrator()
            os.environ["CROSS_SOURCE_DEMO_ENABLED"] = "0"
            disabled = build_default_orchestrator()
            self.assertFalse(disabled.registry.has("cross_source_demo"))
            for question in ("工单 WO-2026-0101 是否按时完成？", "客户总数是多少？", "报销制度上限是多少？"):
                self.assertEqual(route(question, default.registry), route(question, disabled.registry))
                answer = disabled.answer(question, "disabled-route-test")
                self.assertFalse(any(step.tool.startswith("cross_source_") for step in answer.trace))

    def test_enabled_demo_switch_only_changes_work_order_route(self) -> None:
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_ENABLED": "0", "DOC_SEARCH_BACKEND": "mock", "GRAPH_BACKEND": "mock"}):
            disabled = build_default_orchestrator()
            os.environ["CROSS_SOURCE_DEMO_ENABLED"] = "1"
            enabled = build_default_orchestrator()
            self.assertEqual(route("WO-2026-0101", enabled.registry).steps, [("cross_source_demo", "WO-2026-0101")])
            for question in ("客户总数是多少？", "报销制度上限是多少？", "你好"):
                self.assertEqual(route(question, disabled.registry), route(question, enabled.registry))

    def test_work_order_demo_route_has_valid_private_fact_dependencies(self) -> None:
        with patch.dict(os.environ, {"CROSS_SOURCE_DEMO_ENABLED": "1", "DOC_SEARCH_BACKEND": "mock", "GRAPH_BACKEND": "mock"}):
            enabled = build_default_orchestrator()

        plan = route("工单 WO-2026-0001 是否在合同修复时限内完成？", enabled.registry)

        self.assertEqual(plan.steps, [("cross_source_demo", "WO-2026-0001")])
        self.assertIsNotNone(plan.fact_plan)
        self.assertEqual(
            plan.fact_plan.validate(),
            [
                "work_order_facts",
                "controlled_contract_mapping",
                "repair_duration",
                "contract_repair_limit",
                "sla_verdict",
            ],
        )


if __name__ == "__main__":
    unittest.main()
