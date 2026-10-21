"""编排层私有证据账本：候选命中与最终采用证据必须分开。"""

import unittest

from app.contracts import DocRef, SqlRef, TraceStep
from app.evidence_ledger import (
    EvidenceLedger,
    FactRecord,
    FactTask,
    evidence_key,
    ledger_for_response,
)


def doc_ref(doc_id: str, start: int, end: int) -> DocRef:
    return DocRef(
        kind="doc",
        doc_id=doc_id,
        snippet=f"片段 {start}-{end}",
        char_start=start,
        char_end=end,
    )


class EvidenceLedgerTests(unittest.TestCase):
    def test_trace_candidates_are_not_automatically_selected_as_response_evidence(self) -> None:
        candidate = doc_ref("policy", 0, 5)
        selected = doc_ref("policy", 10, 15)
        trace = [TraceStep(
            step_id=1,
            tool="doc_search",
            input={},
            output={},
            elapsed_ms=1,
            hit=[candidate, selected],
        )]

        ledger = ledger_for_response([selected], trace)

        self.assertEqual(ledger.response_refs(), [selected])
        self.assertIn(evidence_key(candidate), ledger.candidates)
        self.assertNotIn(evidence_key(candidate), ledger.response_evidence_ids)
        self.assertEqual(ledger.claim_links, {})

    def test_explicit_claim_binding_requires_selected_evidence(self) -> None:
        candidate = doc_ref("policy", 0, 5)
        selected = doc_ref("policy", 10, 15)
        ledger = ledger_for_response([selected], [TraceStep(
            step_id=1,
            tool="doc_search",
            input={},
            output={},
            elapsed_ms=1,
            hit=[candidate, selected],
        )])

        with self.assertRaisesRegex(ValueError, "未进入最终响应"):
            ledger.bind_claim("claim-1", [evidence_key(candidate)])

        ledger.bind_claim("claim-1", [evidence_key(selected)])
        self.assertEqual(ledger.claim_links["claim-1"].evidence_ids, (evidence_key(selected),))

    def test_empty_tool_refs_preserve_trace_fallback_and_deduplication(self) -> None:
        sql_ref = SqlRef(
            kind="sql", table="sales.orders", column="order_id", row_pk="42", snippet="订单 42"
        )
        trace = [
            TraceStep(step_id=1, tool="sql", input={}, output={}, elapsed_ms=1, hit=[sql_ref]),
            TraceStep(step_id=2, tool="sql", input={}, output={}, elapsed_ms=1, hit=[sql_ref]),
        ]

        ledger = ledger_for_response([], trace)

        self.assertEqual(ledger.response_refs(), [sql_ref])
        self.assertEqual(ledger.response_evidence_ids, [evidence_key(sql_ref)])

    def test_same_location_prefers_full_source_snippet_over_display_excerpt(self) -> None:
        excerpt = doc_ref("policy", 0, 20)
        complete = excerpt.model_copy(update={"snippet": "完整的原始合同条款片段" * 20})
        ledger = EvidenceLedger()

        ledger.add_candidate(excerpt)
        ledger.add_candidate(complete)

        self.assertEqual(ledger.candidates[evidence_key(excerpt)], complete)

    def test_claim_requires_at_least_one_evidence(self) -> None:
        ledger = EvidenceLedger()
        with self.assertRaisesRegex(ValueError, "至少需要一条"):
            ledger.bind_claim("claim-1", [])

    def test_fact_plan_orders_dependencies_and_rejects_cycles(self) -> None:
        ledger = EvidenceLedger()
        ledger.add_task(FactTask("events", "工单受理与恢复时间", ("nl2sql",)))
        ledger.add_task(FactTask("contract", "适用合同与时限", ("doc_search",), ("mapping",)))
        ledger.add_task(FactTask("mapping", "设备对应的合同", ("nl2sql",), ("events",)))
        self.assertEqual(ledger.validate_plan(), ["events", "mapping", "contract"])

        cyclic = EvidenceLedger()
        cyclic.add_task(FactTask("a", "事实 A", depends_on=("b",)))
        cyclic.add_task(FactTask("b", "事实 B", depends_on=("a",)))
        with self.assertRaisesRegex(ValueError, "循环"):
            cyclic.validate_plan()

    def test_supported_fact_requires_source_and_supported_dependencies(self) -> None:
        ledger = EvidenceLedger()
        ledger.add_task(FactTask("identity", "设备与合同映射"))
        ledger.add_task(FactTask("sla", "合同修复时限", depends_on=("identity",)))
        ref = SqlRef(
            kind="sql", table="contracts", column="contract_id", row_pk="C-1", snippet="合同 C-1"
        )
        key = ledger.add_candidate(ref)

        with self.assertRaisesRegex(ValueError, "前置事实未受支持"):
            ledger.record_fact(FactRecord("fact-sla", "sla", "8 小时", "supported", (key,)))
        with self.assertRaisesRegex(ValueError, "至少有一条来源证据"):
            ledger.record_fact(FactRecord("fact-identity", "identity", "DEV-1→C-1", "supported"))

        ledger.record_fact(FactRecord("fact-identity", "identity", "DEV-1→C-1", "supported", (key,)))
        ledger.record_fact(FactRecord("fact-sla", "sla", "8 小时", "supported", (key,)))
        self.assertEqual(ledger.facts["fact-sla"].state, "supported")


if __name__ == "__main__":
    unittest.main()
