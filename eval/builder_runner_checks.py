"""无网络检查：三组知识路由、标准答案隔离、安全执行与澄清分开计分。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_benchmark_runner as runner
from app.nl2sql.executor import ExecutionResult


RAW = {"database": "toy", "dialect": "postgres", "tables": [
    {"schema": "public", "table": "sample", "columns": [
        {"name": "id", "type": "integer", "is_primary_key": True}], "foreign_keys": []}]}


class FakeClient:
    def __init__(self, response: dict[str, str]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user,
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        return json.dumps(self.response)


class FakeExecutor:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, sql: str) -> ExecutionResult:
        self.queries.append(sql)
        return ExecutionResult(rows=[{"n": 1}], columns=["n"], row_count=1)


class RouteTests(unittest.TestCase):
    def test_group_isolation_and_gold_not_sent(self) -> None:
        case = {"id": "toy", "question": "数据有几条？", "kind": "sql",
                "gold_sql": "SELECT 919191 AS forbidden_gold_marker"}
        for arm, marker in (("A", None), ("B", "draft_only_marker"), ("C", "verified_only_marker")):
            entries = [] if marker is None else [{"id": "test", "description": marker,
                "probes": [{"sql": "secret_probe_sql"}], "audit": "secret_review_label"}]
            client = FakeClient({"action": "sql", "sql": "SELECT 1 AS n", "clarification": ""})
            executor = FakeExecutor()
            record = runner.solve(case, arm, 1, RAW, entries, executor,
                ExecutionResult(rows=[{"n": 1}], columns=["n"], row_count=1), "test", client)
            self.assertEqual(len(client.calls), 1)
            prompt = client.calls[0]["system"] + client.calls[0]["user"]
            for forbidden in ("forbidden_gold_marker", "secret_probe_sql", "secret_review_label"):
                self.assertNotIn(forbidden, prompt)
            for other in ("draft_only_marker", "verified_only_marker"):
                self.assertEqual(other in prompt, other == marker)
            self.assertEqual(record["verdict"]["kind"], "pass")
            self.assertEqual(executor.queries, ["SELECT 1 AS n"])

    def test_clarification_does_not_execute_sql(self) -> None:
        case = {"id": "clarify", "question": "金额是多少？", "kind": "clarify",
                "clarification_keywords": [["税", "运费"], ["含", "不含"]]}
        executor = FakeExecutor()
        client = FakeClient({"action": "clarify", "sql": "", "clarification": "金额是否包含税和运费？"})
        result = runner.solve(case, "A", 1, RAW, [], executor, None, "test", client)
        self.assertEqual(result["verdict"]["kind"], "pass")
        self.assertEqual(executor.queries, [])
        case["kind"] = "sql"
        result = runner.solve(case, "A", 1, RAW, [], executor, None, "test", client)
        self.assertEqual(result["verdict"]["kind"], "fail")

    def test_baseline_rejects_knowledge(self) -> None:
        with self.assertRaises(ValueError):
            runner.solve({"question": "x"}, "A", 1, RAW, [{"id": "bad"}], FakeExecutor(), None, "test")

    def test_structured_actions_require_consistency(self) -> None:
        for obj in [{"action": "clarify", "sql": "SELECT 1", "clarification": "缺年份"},
                    {"action": "sql", "sql": "SELECT 1", "clarification": "缺年份"}]:
            with self.assertRaises(ValueError):
                runner.parse_answer(json.dumps(obj))

    def test_case_field_compatibility(self) -> None:
        case = runner.canonical_case({"id": "x", "primary_task": 4, "expected_action": "clarify", "phase": "test"})
        self.assertEqual((case["task"], case["kind"], case["split"]), (4, "clarify", "test"))


if __name__ == "__main__":
    unittest.main()
