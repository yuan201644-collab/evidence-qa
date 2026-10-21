"""无模型网络调用的候选分支回归：防止候选被错误送入基准。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import ontology_experiment as e


class FakeClient:
    def __init__(self, **kwargs: object) -> None:
        self.calls: list[dict] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        if "检索规划器" in system:
            return json.dumps({"query": "销售订单商品金额", "tables": ["Sales.SalesOrderHeader"]})
        return 'SELECT SUM("SubTotal") AS amount FROM "Sales"."SalesOrderHeader"'


class FakeExecutor:
    def execute(self, sql: str) -> e.ExecutionResult:
        return e.ExecutionResult(rows=[{"amount": 12}], columns=["amount"], row_count=1)


class RouteChecks(unittest.TestCase):
    def test_candidate_receives_patch_and_equal_request_budget(self) -> None:
        raw = {"database": "fixture", "dialect": "postgres", "tables": [
            {"schema": "Sales", "table": "SalesOrderHeader", "columns": [{"name": "SubTotal", "type": "numeric"}]},
            {"schema": "Sales", "table": "Unrelated", "columns": [{"name": "x", "type": "int"}]}]}
        entry = {"id": "sales_header", "title": "销售订单商品金额", "aliases": ["销售"],
                 "tables": ["Sales.SalesOrderHeader"], "text": "候选的证据标记：商品金额使用SubTotal", "evidence": ["fixture"]}
        case = {"id": "fixture", "question": "销售订单商品金额合计多少？", "gold_sql": 'SELECT SUM("SubTotal") FROM "Sales"."SalesOrderHeader"'}
        gold = FakeExecutor().execute(case["gold_sql"])
        with patch.object(e, "MeasuredClient", FakeClient):
            results = [e.solve(case, arm, 1, raw, [entry], FakeExecutor(), gold, "check") for arm in ("retrieval", "candidate", "evolved")]
        for result in results:
            self.assertEqual(len(result["calls"]), 2)
            self.assertEqual(result["selected_tables"], ["Sales.SalesOrderHeader"])
            self.assertEqual([t["tool"] for t in result["trace"][:3]], ["normalizer", "browse", "resolve"])
            self.assertIn("候选的证据标记", result["calls"][-1]["user"])
            self.assertTrue(result["verdict"]["kind"] in e.PASS)


if __name__ == "__main__":
    unittest.main()
