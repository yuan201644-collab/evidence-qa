"""评测器不得仅凭状态或散落关键词通过证据检查。"""
import copy
import unittest

from eval.cross_source_runner import (
    _absence_checks_match,
    _row_pk_identity,
    _row_pk_identity_matches,
    _sql_reference_contains_column_value,
    _retrieval_policy_check,
    compare,
    extract_status,
)
from eval.cross_source_pair_runner import experiment_labels


class ComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = {"case_id": "E1", "question": "WO-2026-0105"}
        self.gold = {
            "expected_status": "missing_required_event",
            "expected_missing_events": ["service_restored"],
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "key_event_missing",
            "absence_evidence_required": True,
            "required_absence_refs": [{"table": "cross_source_demo.work_order_events",
                                       "column": "event_type/event_at", "snippets": ["WO-2026-0105", "service_restored"]}],
            "required_absence_checks": [{"table": "cross_source_demo.work_order_events",
                                          "filters": {"work_order_id": "WO-2026-0105",
                                                      "event_type": "service_restored"}}],
        }
        ref = {"kind": "sql", "table": "cross_source_demo.work_order_events",
               "column": "event_type/event_at", "row_pk": None,
               "snippet": "work_order_id=WO-2026-0105 event_type=service_restored 未返回记录"}
        self.response = {"refs": [ref], "trace": [
            {"tool": "cross_source_sql", "input": {
                "query": "SELECT work_order_id, event_type FROM cross_source_demo.work_order_events "
                         "WHERE work_order_id = 'WO-2026-0105' AND event_type = 'service_restored'",
            }, "output": {
                "row_count": 0,
                "absence_checks": [{
                    "table": "cross_source_demo.work_order_events",
                    "filters": {"work_order_id": "WO-2026-0105", "event_type": "service_restored"},
                    "row_count": 0,
                }],
            }, "hit": [ref]},
            {"tool": "cross_source_alignment", "output": {
                "status": "missing_required_event", "missing_events": ["service_restored"],
                "stop_reason_code": "key_event_missing",
            }, "hit": [ref]},
        ]}

    def test_machine_field_value_labels_are_exact_and_escape_separators(self) -> None:
        snippet = '描述 [equipment_name="泵; 状态=active] 1"; status="inactive"]'
        self.assertTrue(_sql_reference_contains_column_value(
            snippet, "equipment_name", "泵; 状态=active] 1"
        ))
        self.assertFalse(_sql_reference_contains_column_value(
            snippet, "equipment_name", "泵"
        ))
        self.assertFalse(_sql_reference_contains_column_value(
            snippet, "status", "active"
        ))

    def test_aware_iso_timestamp_citations_match_by_instant_across_offsets(self) -> None:
        expected = '2026-11-05T09:00:00+08:00'
        self.assertTrue(_sql_reference_contains_column_value(
            ' [opened_at="2026-11-05T01:00:00+00:00"]', "opened_at", expected,
        ))
        self.assertTrue(_sql_reference_contains_column_value(
            ' [opened_at="2026-11-05T01:00:00Z"]', "opened_at", expected,
        ))

    def test_timestamp_matching_stays_strict_for_wrong_or_naive_instants(self) -> None:
        expected = '2026-11-05T09:00:00+08:00'
        self.assertFalse(_sql_reference_contains_column_value(
            ' [opened_at="2026-11-05T02:00:00+00:00"]', "opened_at", expected,
        ))
        self.assertFalse(_sql_reference_contains_column_value(
            ' [opened_at="2026-11-05T09:00:00"]', "opened_at", expected,
        ))
        # Naive timestamps remain literal-only; no timezone is inferred.
        self.assertTrue(_sql_reference_contains_column_value(
            ' [opened_at="2026-11-05T09:00:00"]', "opened_at", '2026-11-05T09:00:00',
        ))

    def test_complete_negative_evidence_passes(self) -> None:
        self.assertTrue(compare(self.case, self.gold, self.response, 0)["automatic_checks_pass"])

    def test_equivalent_composite_primary_key_serializations_match(self) -> None:
        legacy_identity = _row_pk_identity("WO-2026-0105|service_restored")
        keyed_identity = _row_pk_identity(
            "work_order_id=WO-2026-0105&event_type=service_restored"
        )
        self.assertNotEqual(legacy_identity, keyed_identity)
        self.assertTrue(_row_pk_identity_matches(legacy_identity, keyed_identity))
        self.assertNotEqual(
            _row_pk_identity("WO-2026-0105|service_restored"),
            _row_pk_identity("service_restored|WO-2026-0105"),
        )
        self.assertFalse(_row_pk_identity_matches(
            _row_pk_identity("work_order_id=WO-2026-0105&event_type=service_restored"),
            _row_pk_identity("ticket_id=WO-2026-0105&event_type=service_restored"),
        ))

    def test_equivalent_composite_primary_key_citation_is_not_false_negative(self) -> None:
        gold = {**self.gold, "expected_evidence_pks": ["WO-2026-0105|accepted"]}
        ref = {
            "kind": "sql", "table": "cross_source_demo.work_order_events",
            "column": "work_order_id/event_type",
            "row_pk": "work_order_id=WO-2026-0105&event_type=accepted",
            "snippet": "work_order_id=WO-2026-0105 event_type=accepted",
        }
        response = copy.deepcopy(self.response)
        response["refs"].append(ref)
        response["trace"][-1]["hit"].append(ref)
        result = compare(self.case, gold, response, 0)
        self.assertTrue(result["sql_evidence_present"])
        self.assertTrue(result["citation_completeness_pass"])

    def test_structured_composite_primary_key_rejects_same_values_under_different_fields(self) -> None:
        gold = {**self.gold, "expected_evidence_pks": [
            "work_order_id=WO-2026-0105&event_type=accepted",
        ]}
        wrong_key_ref = {
            "kind": "sql", "table": "cross_source_demo.work_order_events",
            "column": "ticket_id/event_type",
            "row_pk": "ticket_id=WO-2026-0105&event_type=accepted",
            "snippet": "ticket_id=WO-2026-0105 event_type=accepted",
        }
        response = copy.deepcopy(self.response)
        response["refs"].append(wrong_key_ref)
        response["trace"][-1]["hit"].append(wrong_key_ref)
        self.assertFalse(compare(self.case, gold, response, 0)["sql_evidence_present"])

    def test_required_absence_column_may_be_one_component_of_reference(self) -> None:
        gold = copy.deepcopy(self.gold)
        gold["required_absence_refs"][0]["column"] = "event_type"
        result = compare(self.case, gold, self.response, 0)
        self.assertTrue(result["absence_evidence_present"])

    def test_wrong_filter_in_same_table_does_not_pass(self) -> None:
        self.response["refs"][0]["snippet"] = "WO-2026-9999 service_restored"
        self.assertFalse(compare(self.case, self.gold, self.response, 0)["absence_evidence_present"])

    def test_unlocated_reference_does_not_pass(self) -> None:
        del self.response["refs"][0]["table"]
        self.assertFalse(compare(self.case, self.gold, self.response, 0)["automatic_checks_pass"])

    def test_reference_not_in_trace_does_not_pass(self) -> None:
        for step in self.response["trace"]:
            step["hit"] = []
        self.assertFalse(compare(self.case, self.gold, self.response, 0)["refs_trace_consistent"])

    def test_early_stop_requires_located_sql_evidence_on_alignment_trace(self) -> None:
        self.response["trace"][-1]["hit"] = []
        _ok, errors = _retrieval_policy_check(self.gold, self.response)
        self.assertIn("early_stop_evidence_not_trace_backed", errors)

    def test_unselected_retrieval_candidate_may_remain_in_trace(self) -> None:
        candidate = {
            "kind": "doc", "doc_id": "HT-OTHER", "snippet": "同一索引中的未选候选",
            "section_path": "服务条款",
        }
        self.response["trace"].append({
            "tool": "rerank", "output": {}, "hit": [candidate],
        })
        self.assertTrue(compare(self.case, self.gold, self.response, 0)["refs_trace_consistent"])

    def test_early_stop_trace_exposes_business_status_separately_from_reason(self) -> None:
        response = {"trace": [{
            "tool": "cross_source_alignment",
            "output": {"status": "not_determined", "stop_reason_code": "event_time_reversed"},
        }]}
        self.assertEqual(extract_status(response), "not_determined")

    def test_legacy_hybrid_retrieve_trace_counts_for_present_clause(self) -> None:
        ref = {"kind": "doc", "doc_id": "HT-1", "snippet": "修复时限 8 小时", "section_path": "SLA"}
        response = {"trace": [{
            "tool": "hybrid_retrieve", "input": {"query": "HT-1 修复时限"},
            "output": {"num_hits": 1}, "hit": [ref],
        }]}
        gold = {
            "expected_retrieval_state": "clause_present_expected",
            "expected_doc_ids": ["HT-1"],
            "required_doc_snippets": ["修复时限 8 小时"],
        }
        self.assertTrue(_retrieval_policy_check(gold, response)[0])

    def test_docref_in_graph_hit_does_not_count_as_real_rag_citation(self) -> None:
        doc_ref = {
            "kind": "doc", "doc_id": "HT-1", "doc_title": "合同",
            "section_path": "修复时限", "snippet": "修复时限 8 小时",
        }
        response = {
            "answer": "合成回答",
            "refs": [doc_ref],
            "trace": [
                {"tool": "graph_reason", "output": {}, "hit": [doc_ref]},
                {"tool": "hybrid_retrieve", "output": {"num_hits": 0}, "hit": []},
                {"tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": []},
            ],
        }
        gold = {
            "expected_status": "supported",
            "expected_retrieval_state": "clause_present_expected",
            "expected_doc_ids": ["HT-1"],
            "required_doc_snippets": ["修复时限 8 小时"],
        }
        result = compare(self.case, gold, response, 0)
        self.assertTrue(result["refs_trace_consistent"])
        self.assertFalse(result["document_refs_from_actual_retrieval"])
        self.assertFalse(result["citation_completeness_pass"])

    def test_real_new_arm_rag_hits_count_when_projection_has_no_hits(self) -> None:
        doc_ref = {
            "kind": "doc", "doc_id": "HT-1", "doc_title": "合同",
            "section_path": "修复时限", "snippet": "修复时限 8 小时",
        }
        response = {
            "refs": [doc_ref],
            "trace": [
                {"tool": "hybrid_retrieve", "output": {"num_hits": 1}, "hit": [doc_ref]},
                {"tool": "rerank", "output": {"order": [0]}, "hit": [doc_ref]},
                {"tool": "cross_source_doc_retrieval", "output": {
                    "attempts": [{"strategy": "hybrid_bm25_vector", "status": "candidates_retrieved",
                                  "candidate_count": 1}],
                }, "hit": []},
            ],
        }
        gold = {
            "expected_status": "supported",
            "expected_retrieval_state": "clause_present_expected",
            "expected_doc_ids": ["HT-1"],
            "required_doc_snippets": ["修复时限 8 小时"],
            "expected_retrieval_attempts": 1,
            "expected_retrieval_statuses": ["candidates_retrieved"],
        }
        result = compare(self.case, gold, response, 0)
        self.assertTrue(result["document_refs_from_actual_retrieval"])
        self.assertTrue(result["citation_completeness_pass"])

    def test_projection_only_docref_does_not_count_as_real_rag_citation(self) -> None:
        doc_ref = {
            "kind": "doc", "doc_id": "HT-1", "doc_title": "合同",
            "section_path": "修复时限", "snippet": "修复时限 8 小时",
        }
        response = {
            "refs": [doc_ref],
            "trace": [{"tool": "cross_source_doc_retrieval", "output": {
                "attempts": [{"strategy": "hybrid_bm25_vector", "status": "candidates_retrieved",
                              "candidate_count": 1}],
            }, "hit": [doc_ref]}],
        }
        gold = {
            "expected_status": "supported",
            "expected_retrieval_state": "clause_present_expected",
            "expected_doc_ids": ["HT-1"],
            "required_doc_snippets": ["修复时限 8 小时"],
            "expected_retrieval_attempts": 1,
            "expected_retrieval_statuses": ["candidates_retrieved"],
        }
        result = compare(self.case, gold, response, 0)
        self.assertFalse(result["document_refs_from_actual_retrieval"])
        self.assertFalse(result["citation_completeness_pass"])

    def test_legacy_hybrid_retrieve_is_rejected_after_expected_early_stop(self) -> None:
        response = {"trace": [{
            "tool": "hybrid_retrieve", "input": {}, "output": {"num_hits": 0}, "hit": [],
            }, {
            "tool": "cross_source_alignment", "output": {
                "status": "not_determined", "stop_reason_code": "customer_scope_conflict",
            }, "hit": [],
        }]}
        gold = {
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "customer_scope_conflict",
        }
        ok, errors = _retrieval_policy_check(gold, response)
        self.assertFalse(ok)
        self.assertIn("document_retrieval_after_early_stop", errors)

    def test_right_status_wrong_number_does_not_pass(self) -> None:
        gold = copy.deepcopy(self.gold)
        gold["expected_metric_checks"] = [{"metric": "service_restored", "elapsed_seconds": 14400}]
        self.response["trace"][0]["output"]["metric_checks"] = [{"metric": "service_restored", "elapsed_seconds": 1}]
        self.assertFalse(compare(self.case, gold, self.response, 0)["metric_values_match"])

    def test_supported_runtime_status_maps_to_matching_business_sla_status(self) -> None:
        metric = {
            "start_event": "accepted", "end_event": "service_restored",
            "elapsed_seconds": 3600, "limit_hours": 4, "within_limit": True,
        }
        response = {"refs": [], "trace": [
            {"tool": "cross_source_doc_retrieval", "output": {"attempts": []}},
            {"tool": "cross_source_alignment", "output": {
                "status": "supported",
                "metric_checks": [{
                    "metric": "service_restored", "start_event": "accepted",
                    "end_event": "service_restored", "elapsed_seconds": 3600,
                    "limit": 4, "unit": "hours", "within_limit": True,
                }],
            }, "hit": []},
        ]}
        gold = {
            "expected_status": "repair_within_limit",
            "expected_retrieval_state": "clause_present_expected",
            "required_metric": metric,
        }
        result = compare(self.case, gold, response, 0)
        self.assertEqual(result["actual_status"], "supported")
        self.assertTrue(result["status_match"])
        self.assertTrue(result["metric_values_match"])
        self.assertTrue(result["correctness_pass"])

    def test_supported_status_does_not_map_with_duplicate_or_mismatched_sla_metric(self) -> None:
        gold = {
            "expected_status": "repair_over_limit",
            "expected_retrieval_state": "clause_present_expected",
            "required_metric": {
                "start_event": "accepted", "end_event": "service_restored",
                "elapsed_seconds": 18000, "limit_hours": 4, "within_limit": False,
            },
        }
        valid_metric = {
            "metric": "service_restored", "start_event": "accepted",
            "end_event": "service_restored", "elapsed_seconds": 18000,
            "limit": 4, "unit": "hours", "within_limit": False,
        }
        valid_response = {"refs": [], "trace": [
            {"tool": "cross_source_doc_retrieval", "output": {"attempts": []}},
            {"tool": "cross_source_alignment", "output": {
                "status": "supported", "metric_checks": [valid_metric],
            }, "hit": []},
        ]}
        valid_result = compare(self.case, gold, valid_response, 0)
        self.assertTrue(valid_result["status_match"])
        self.assertTrue(valid_result["metric_values_match"])
        for metric_checks in (
            [{**valid_metric, "elapsed_seconds": 1}],
            [valid_metric, dict(valid_metric)],
        ):
            response = {"refs": [], "trace": [
                {"tool": "cross_source_doc_retrieval", "output": {"attempts": []}},
                {"tool": "cross_source_alignment", "output": {
                    "status": "supported", "metric_checks": metric_checks,
                }, "hit": []},
            ]}
            result = compare(self.case, gold, response, 0)
            self.assertFalse(result["status_match"])
            self.assertFalse(result["metric_values_match"])

    def test_exact_legacy_repair_status_remains_compatible(self) -> None:
        result = compare(self.case, {
            "expected_status": "repair_within_limit",
        }, {"refs": [], "trace": [{"tool": "cross_source_alignment", "output": {
            "status": "repair_within_limit",
        }, "hit": []}]}, 0)
        self.assertTrue(result["status_match"])

    def test_natural_language_only_answer_marks_automatic_status_unavailable(self) -> None:
        result = compare(
            self.case,
            {"expected_status": "repair_within_limit"},
            {"answer": "工单满足合同修复时限。", "refs": [], "trace": []},
            0,
        )
        self.assertIsNone(result["actual_status"])
        self.assertFalse(result["status_comparison_available"])
        # This remains a failed automatic status check, not a human correctness label.
        self.assertFalse(result["status_match"])

    def test_not_determined_still_requires_allowlisted_structured_reason(self) -> None:
        gold = {
            "expected_status": "not_determined",
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "event_time_reversed",
            "absence_evidence_required": False,
        }
        for reason in (None, "invented_reason"):
            output = {"status": "not_determined"}
            if reason is not None:
                output["stop_reason_code"] = reason
            response = {"refs": [], "trace": [{
                "tool": "cross_source_alignment", "output": output, "hit": [],
            }]}
            result = compare(self.case, gold, response, 0)
            self.assertTrue(result["status_match"])
            self.assertFalse(result["trace_compliance_pass"])
            self.assertIn(
                "not_determined_requires_allowlisted_reason",
                result["retrieval_policy_errors"],
            )

    def test_sql_reference_normalizes_device_label_but_still_requires_exact_identifier(self) -> None:
        gold = copy.deepcopy(self.gold)
        gold["required_sql_snippets"] = ["设备 EQ-734，名称 石桥能耗网关（南仓站），所属客户 CUS-44"]
        ref = {
            "kind": "sql", "table": "cross_source_demo.equipment",
            "column": "equipment_name/customer_id", "row_pk": "EQ-734",
            "snippet": "设备编号 EQ-734，名称 石桥能耗网关（南仓站），所属客户 CUS-44。",
        }
        self.response["refs"].append(ref)
        self.response["trace"][0]["hit"].append(ref)
        self.assertTrue(compare(self.case, gold, self.response, 0)["required_sql_snippets_present"])

        self.response["refs"][-1]["snippet"] = "设备编号 EQ-735，名称 石桥能耗网关（南仓站），所属客户 CUS-44。"
        self.response["trace"][0]["hit"][-1]["snippet"] = self.response["refs"][-1]["snippet"]
        self.assertFalse(compare(self.case, gold, self.response, 0)["required_sql_snippets_present"])

    def test_structured_sql_ref_requirement_passes_when_one_ref_covers_all_fields(self) -> None:
        requirement = {
            "table": "cross_source_demo.work_order_events",
            "row_pk": "WO-1|service_restored",
            "required_columns": ["work_order_id", "event_type", "event_at"],
            "required_values": {
                "work_order_id": "WO-1", "event_type": "service_restored",
                "event_at": "2026-10-01T09:00:00",
            },
        }
        ref = {
            "kind": "sql", "table": "cross_source_demo.work_order_events",
            "column": "work_order_id/event_type/event_at",
            "row_pk": "work_order_id=WO-1&event_type=service_restored",
            "snippet": "work_order_id=WO-1 event_type=service_restored event_at=2026-10-01T09:00:00",
        }
        response = {"refs": [ref], "trace": [{
            "tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": [ref],
        }]}
        result = compare(self.case, {
            "expected_status": "supported", "required_sql_refs": [requirement],
        }, response, 0)
        self.assertTrue(result["required_sql_refs_present"])

    def test_structured_sql_ref_does_not_join_columns_or_values_across_refs(self) -> None:
        requirement = {
            "table": "cross_source_demo.work_order_events",
            "row_pk": "WO-1|service_restored",
            "required_columns": ["work_order_id", "event_type", "event_at"],
            "required_values": {
                "work_order_id": "WO-1", "event_type": "service_restored",
                "event_at": "2026-10-01T09:00:00",
            },
        }
        refs = [
            {
                "kind": "sql", "table": "cross_source_demo.work_order_events",
                "column": "work_order_id/event_type",
                "row_pk": "WO-1|service_restored",
                "snippet": "work_order_id=WO-1 event_type=service_restored",
            },
            {
                "kind": "sql", "table": "cross_source_demo.work_order_events",
                "column": "event_at", "row_pk": "WO-1|service_restored",
                "snippet": "event_at=2026-10-01T09:00:00",
            },
        ]
        response = {"refs": refs, "trace": [{
            "tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": refs,
        }]}
        result = compare(self.case, {
            "expected_status": "supported", "required_sql_refs": [requirement],
        }, response, 0)
        self.assertFalse(result["required_sql_refs_present"])

    def test_structured_sql_ref_rejects_value_under_the_wrong_column(self) -> None:
        requirement = {
            "table": "cross_source_demo.work_order_events",
            "row_pk": "WO-1|service_restored",
            "required_columns": ["work_order_id", "event_type", "event_at"],
            "required_values": {
                "work_order_id": "WO-1", "event_type": "service_restored",
                "event_at": "2026-10-01T09:00:00",
            },
        }
        ref = {
            "kind": "sql", "table": "cross_source_demo.work_order_events",
            "column": "work_order_id/event_type/event_at",
            "row_pk": "WO-1|service_restored",
            "snippet": "work_order_id=WO-1 event_type=service_restored restored_at=2026-10-01T09:00:00",
        }
        response = {"refs": [ref], "trace": [{
            "tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": [ref],
        }]}
        result = compare(self.case, {
            "expected_status": "supported", "required_sql_refs": [requirement],
        }, response, 0)
        self.assertFalse(result["required_sql_refs_present"])

    def test_structured_sql_ref_requires_field_labels_for_chinese_prose_snippets(self) -> None:
        requirement = {
            "table": "cross_source_demo.equipment", "row_pk": "EQ-1",
            "required_columns": ["equipment_id", "customer_name"],
            "required_values": {"equipment_id": "EQ-1", "customer_name": "南仓站"},
        }
        ref = {
            "kind": "sql", "table": "cross_source_demo.equipment",
            "column": "equipment_id/customer_name", "row_pk": "EQ-1",
            "snippet": "设备编号 EQ-1，设备名称石桥能耗网关，所属客户南仓站。",
        }
        response = {"refs": [ref], "trace": [{
            "tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": [ref],
        }]}
        result = compare(self.case, {
            "expected_status": "supported", "required_sql_refs": [requirement],
        }, response, 0)
        # A prose-only snippet cannot prove which target column owns each value.
        self.assertFalse(result["required_sql_refs_present"])

    def test_structured_sql_ref_rejects_swapped_column_values(self) -> None:
        requirement = {
            "table": "cross_source_demo.work_order_events",
            "row_pk": "WO-1|service_restored",
            "required_columns": ["work_order_id", "event_type", "event_at"],
            "required_values": {
                "work_order_id": "WO-1", "event_type": "service_restored",
                "event_at": "2026-10-01T09:00:00",
            },
        }
        ref = {
            "kind": "sql", "table": "cross_source_demo.work_order_events",
            "column": "work_order_id/event_type/event_at",
            "row_pk": "work_order_id=WO-1&event_type=service_restored",
            "snippet": (
                "work_order_id=WO-1 event_type=2026-10-01T09:00:00 "
                "event_at=service_restored"
            ),
        }
        response = {"refs": [ref], "trace": [{
            "tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": [ref],
        }]}
        result = compare(self.case, {
            "expected_status": "supported", "required_sql_refs": [requirement],
        }, response, 0)
        self.assertFalse(result["required_sql_refs_present"])

    def test_legacy_and_structured_sql_requirements_are_combined(self) -> None:
        requirement = {
            "table": "cross_source_demo.equipment", "row_pk": "EQ-1",
            "required_columns": ["equipment_id"], "required_values": {"equipment_id": "EQ-1"},
        }
        ref = {
            "kind": "sql", "table": "cross_source_demo.equipment", "column": "equipment_id",
            "row_pk": "EQ-1", "snippet": "equipment_id=EQ-1",
        }
        response = {"refs": [ref], "trace": [{
            "tool": "cross_source_alignment", "output": {"status": "supported"}, "hit": [ref],
        }]}
        result = compare(self.case, {
            "expected_status": "supported",
            "required_sql_snippets": ["must remain present"],
            "required_sql_refs": [requirement],
        }, response, 0)
        self.assertTrue(result["required_sql_refs_present"])
        self.assertFalse(result["required_sql_snippets_present"])
        self.assertFalse(result["citation_completeness_pass"])

    def test_absence_check_requires_executor_metadata_not_just_claimed_zero(self) -> None:
        expected = [{
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-MED-306"},
        }]
        negative_ref = {
            "kind": "sql", "table": "cross_source_demo.equipment", "column": "equipment_id",
            "row_pk": None, "snippet": "按 equipment_id=DEV-MED-306 查询设备表未返回记录（0 行）。",
        }
        response = {
            "refs": [negative_ref],
            "trace": [{"tool": "cross_source_sql", "input": {"query": (
                "SELECT equipment_id FROM cross_source_demo.equipment "
                "WHERE equipment_id = 'DEV-MED-306'"
            )}, "output": {"row_count": 0, "absence_checks": [{
                "table": "cross_source_demo.equipment",
                "filters": {"equipment_id": "DEV-MED-306"}, "row_count": 1,
            }]}}],
        }
        self.assertFalse(_absence_checks_match(response, expected))
        response["trace"][0]["output"]["absence_checks"][0]["row_count"] = 0
        self.assertTrue(_absence_checks_match(response, expected))
        response["trace"][0]["output"]["absence_checks"].append({
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-MED-306"}, "row_count": 1,
        })
        self.assertFalse(_absence_checks_match(response, expected))
        response["trace"][0]["output"]["absence_checks"].pop()
        response["refs"][0]["snippet"] = "按 guessed_name=DEV-MED-306 查询设备表未返回记录。"
        self.assertFalse(_absence_checks_match(response, expected))

    def test_absence_reference_requires_exact_filter_tokens_and_all_filters(self) -> None:
        expected = [{
            "table": "cross_source_demo.work_order_events",
            "filters": {"work_order_id": "WO-2026-3105", "event_type": "accepted"},
        }]
        response = {"refs": [{
            "kind": "sql", "table": "cross_source_demo.work_order_events", "row_pk": None,
            "snippet": "work_order_id=WO-2026-3105 event_type=accepted 未返回记录",
        }], "trace": [{"tool": "cross_source_sql", "input": {"query": (
            "SELECT work_order_id, event_type FROM cross_source_demo.work_order_events "
            "WHERE work_order_id = 'WO-2026-3105' AND event_type = 'accepted'"
        )}, "output": {"row_count": 0, "absence_checks": [{
            "table": "cross_source_demo.work_order_events",
            "filters": {"work_order_id": "WO-2026-3105", "event_type": "accepted"},
            "row_count": 0,
        }]}}]}
        self.assertTrue(_absence_checks_match(response, expected))
        for snippet in (
            "x_work_order_id=WO-2026-3105 event_type=accepted 未返回记录",
            "work_order_id=WO-2026-3105A event_type=accepted 未返回记录",
            "work_order_id=WO-2026-3105 event_type=accepted_extra 未返回记录",
            "work_order_id=WO-2026-3105 未返回记录",
        ):
            response["refs"][0]["snippet"] = snippet
            self.assertFalse(_absence_checks_match(response, expected), snippet)

    def test_absence_check_requires_metadata_and_reference_as_separate_conditions(self) -> None:
        expected = [{
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-MED-306"},
        }]
        valid_ref = {
            "kind": "sql", "table": "cross_source_demo.equipment", "row_pk": None,
            "snippet": "equipment_id=DEV-MED-306 未返回记录",
        }
        valid_metadata = {
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-MED-306"}, "row_count": 0,
        }

        # SQL 执行元数据可信，但引用缺失/不匹配时不能通过。
        query = (
            "SELECT equipment_id FROM cross_source_demo.equipment "
            "WHERE equipment_id = 'DEV-MED-306'"
        )
        response = {"refs": [], "trace": [{
            "tool": "cross_source_sql", "input": {"query": query}, "output": {
                "row_count": 0, "absence_checks": [copy.deepcopy(valid_metadata)],
            },
        }]}
        self.assertFalse(_absence_checks_match(response, expected))

        # 引用完整，但缺少对应执行元数据时也不能通过。
        response = {"refs": [copy.deepcopy(valid_ref)], "trace": [{
            "tool": "cross_source_sql", "input": {"query": query},
            "output": {"row_count": 0, "absence_checks": []},
        }]}
        self.assertFalse(_absence_checks_match(response, expected))

    def test_legacy_absence_metadata_must_match_executed_sql_and_integer_row_count(self) -> None:
        expected = [{
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-MED-306"},
        }]
        reference = {
            "kind": "sql", "table": "cross_source_demo.equipment", "row_pk": None,
            "snippet": "按 equipment_id=DEV-MED-306 查询设备主数据未返回记录（0 行）。",
        }
        query = (
            "SELECT equipment_id FROM cross_source_demo.equipment "
            "WHERE equipment_id = 'DEV-MED-306'"
        )
        metadata = {
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-MED-306"}, "row_count": 0,
        }
        response = {
            "refs": [reference],
            "trace": [{"tool": "cross_source_sql", "input": {"query": query},
                       "output": {"row_count": 0, "absence_checks": [copy.deepcopy(metadata)]}}],
        }
        self.assertTrue(_absence_checks_match(response, expected))

        for change in (
            {"actual_row_count": True},
            {"check_row_count": True},
            {"actual_row_count": 1},
            {"query": "SELECT customer_id FROM cross_source_demo.equipment WHERE customer_id = 'DEV-MED-306'"},
            {
                "query": (
                    "SELECT equipment_id FROM cross_source_demo.equipment "
                    "WHERE equipment_id = 'DEV-MED-306' AND status = 'active'"
                ),
            },
        ):
            candidate = copy.deepcopy(response)
            output = candidate["trace"][0]["output"]
            if "actual_row_count" in change:
                output["row_count"] = change["actual_row_count"]
            if "check_row_count" in change:
                output["absence_checks"][0]["row_count"] = change["check_row_count"]
            if "query" in change:
                candidate["trace"][0]["input"]["query"] = change["query"]
            self.assertFalse(_absence_checks_match(candidate, expected), change)

    def test_legacy_absence_check_binds_parameterized_sql_from_trace_input(self) -> None:
        expected = [{
            "table": "cross_source_demo.work_orders",
            "filters": {"work_order_id": "WO-2026-9999"},
        }]
        response = {
            "refs": [{
                "kind": "sql", "table": "cross_source_demo.work_orders", "row_pk": None,
                "snippet": "work_order_id=WO-2026-9999 未返回记录（0 行）。",
            }],
            "trace": [{
                "tool": "cross_source_sql",
                "input": {
                    "query": "SELECT work_order_id FROM cross_source_demo.work_orders WHERE work_order_id = %s",
                    "work_order_id": "WO-2026-9999",
                },
                "output": {"row_count": 0, "absence_checks": [{
                    "table": "cross_source_demo.work_orders",
                    "filters": {"work_order_id": "WO-2026-9999"}, "row_count": 0,
                }]},
            }],
        }
        self.assertTrue(_absence_checks_match(response, expected))

    def test_fact_plan_absence_metadata_requires_execution_marker(self) -> None:
        expected = [{
            "table": "contract_equipment",
            "filters": {"equipment_id": "PUMP-RW-7210"},
        }]
        response = {
            "refs": [{
                "kind": "sql", "table": "contract_equipment", "column": "equipment_id",
                "row_pk": None,
                "snippet": "按 contract_equipment.equipment_id=PUMP-RW-7210 精确查询，数据库实际返回 0 行。",
            }],
            "trace": [{
                "tool": "cross_source_absence_check",
                "output": {
                    "table": "contract_equipment",
                    "filters": {"equipment_id": "PUMP-RW-7210"},
                    "row_count": 0,
                    "derived_from_executed_query": True,
                },
            }],
        }
        self.assertTrue(_absence_checks_match(response, expected))
        response["trace"][0]["output"]["derived_from_executed_query"] = False
        self.assertFalse(_absence_checks_match(response, expected))

    def test_bounded_not_found_requires_all_expected_retrieval_attempts(self) -> None:
        gold = {
            "expected_retrieval_state": "bounded_not_found",
            "expected_retrieval_attempts": 2,
            "expected_retrieval_strategies": ["primary_sla_query", "targeted_repair_sla_retry"],
            "expected_retrieval_statuses": ["required_metric_not_found"] * 2,
            "expected_candidate_counts": [1, 1],
        }
        response = {"trace": [{"tool": "cross_source_doc_retrieval", "output": {"attempts": [
            {"strategy": "primary_sla_query", "status": "required_metric_not_found", "candidate_count": 1},
            {"strategy": "targeted_repair_sla_retry", "status": "required_metric_not_found", "candidate_count": 1},
        ]}}], "refs": []}
        self.assertTrue(_retrieval_policy_check(gold, response)[0])
        response["trace"][0]["output"]["attempts"].pop()
        self.assertFalse(_retrieval_policy_check(gold, response)[0])

    def test_m14_early_stop_requires_allowlisted_reason_and_no_doc_retrieval(self) -> None:
        gold = {
            "expected_status": "not_determined",
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "mapping_basis_unapproved",
            "absence_evidence_required": False,
        }
        ref = {"kind": "sql", "table": "cross_source_demo.contract_equipment",
               "column": "equipment_id/contract_id/mapping_basis", "row_pk": "HT-1|DEV-1",
               "snippet": "DEV-1 映射依据 equipment_name_guess，非受控依据"}
        response = {"trace": [{"tool": "cross_source_alignment", "output": {
            "status": "not_determined", "stop_reason_code": "mapping_basis_unapproved",
        }, "hit": [ref]}], "refs": [ref]}
        self.assertTrue(_retrieval_policy_check(gold, response)[0])
        response["trace"].append({"tool": "cross_source_doc_retrieval", "output": {"status": "retrieved"}})
        self.assertFalse(_retrieval_policy_check(gold, response)[0])

    def test_ambiguous_contracts_are_an_allowlisted_early_stop_reason(self) -> None:
        gold = {
            "expected_status": "not_determined",
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "ambiguous_active_contracts",
            "absence_evidence_required": False,
        }
        ref = {"kind": "sql", "table": "cross_source_demo.contract_equipment",
               "column": "equipment_id/contract_id", "row_pk": "HT-1|DEV-1",
               "snippet": "DEV-1 关联多个有效合同"}
        response = {"trace": [{"tool": "cross_source_alignment", "output": {
            "status": "not_determined", "stop_reason_code": "ambiguous_active_contracts",
        }, "hit": [ref]}], "refs": [ref]}
        ok, errors = _retrieval_policy_check(gold, response)
        self.assertTrue(ok)
        self.assertEqual(errors, [])
        response["trace"][0]["output"].pop("stop_reason_code")
        ok, errors = _retrieval_policy_check(gold, response)
        self.assertFalse(ok)
        self.assertIn("early_stop_reason_mismatch", errors)

    def test_unallowlisted_stop_reason_is_rejected_even_when_retrieval_was_expected(self) -> None:
        gold = {
            "expected_retrieval_state": "clause_present_expected",
            "expected_retrieval_statuses": ["required_metric_found"],
        }
        response = {"trace": [
            {"tool": "cross_source_doc_retrieval", "output": {"attempts": [
                {"status": "required_metric_found"},
            ]}},
            {"tool": "cross_source_alignment", "output": {
                "status": "repair_within_limit", "stop_reason_code": "free_text_reason",
            }},
        ], "refs": []}
        ok, errors = _retrieval_policy_check(gold, response)
        self.assertFalse(ok)
        self.assertIn("actual_stop_reason_not_allowlisted", errors)

    def test_allowlisted_early_stop_cannot_coexist_with_expected_document_retrieval(self) -> None:
        gold = {
            "expected_retrieval_state": "clause_present_expected",
            "expected_retrieval_statuses": ["required_metric_found"],
        }
        response = {"trace": [
            {"tool": "cross_source_doc_retrieval", "output": {"attempts": [
                {"status": "required_metric_found"},
            ]}},
            {"tool": "cross_source_alignment", "output": {
                "status": "repair_within_limit", "stop_reason_code": "validity_window_not_cover",
            }},
        ], "refs": []}
        ok, errors = _retrieval_policy_check(gold, response)
        self.assertFalse(ok)
        self.assertIn("early_stop_reason_conflicts_with_expected_retrieval", errors)

    def test_historical_gold_without_retrieval_state_still_checks_actual_stop_reason(self) -> None:
        response = {"trace": [{"tool": "cross_source_alignment", "output": {
            "status": "repair_within_limit", "stop_reason_code": "free_text_reason",
        }}], "refs": []}
        ok, errors = _retrieval_policy_check({"expected_status": "repair_within_limit"}, response)
        self.assertFalse(ok)
        self.assertIn("actual_stop_reason_not_allowlisted", errors)

    def test_early_stop_policy_violation_is_trace_failure_not_answer_failure(self) -> None:
        ref = {
            "kind": "sql", "table": "cross_source_demo.equipment", "column": "equipment_id",
            "row_pk": "DEV-MED-306", "snippet": "设备 DEV-MED-306 主数据已查询。",
        }
        response = {
            "answer": "无法判断，设备主数据缺失。",
            "refs": [ref],
            "trace": [{
                "tool": "cross_source_alignment",
                "output": {"status": "missing_reference_data", "stop_reason_code": "unapproved_reason"},
                "hit": [ref],
            }],
        }
        gold = {
            "expected_status": "missing_reference_data",
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "equipment_master_missing",
            "expected_sql_pks": ["DEV-MED-306"],
        }
        result = compare(self.case, gold, response, 0)
        self.assertTrue(result["correctness_pass"])
        self.assertTrue(result["citation_completeness_pass"])
        self.assertFalse(result["trace_compliance_pass"])

    def test_required_doc_snippet_must_come_from_expected_document(self) -> None:
        expected_ref = {"kind": "doc", "doc_id": "DOC-A", "snippet": "服务范围说明",
                        "section_path": "1.1"}
        unrelated_ref = {"kind": "doc", "doc_id": "DOC-B", "snippet": "修复时限 6 小时",
                         "section_path": "2.1"}
        response = {
            "refs": [expected_ref, unrelated_ref],
            "trace": [
                {"tool": "cross_source_doc_retrieval", "output": {},
                 "hit": [expected_ref, unrelated_ref]},
                {"tool": "cross_source_alignment", "output": {"status": "repair_within_limit"},
                 "hit": [expected_ref, unrelated_ref]},
            ],
        }
        gold = {
            "expected_status": "repair_within_limit",
            "expected_retrieval_state": "clause_present_expected",
            "expected_doc_ids": ["DOC-A"],
            "required_sql_snippets": [],
            "required_doc_snippets": ["修复时限 6 小时"],
        }
        result = compare(self.case, gold, response, 0)
        self.assertTrue(result["document_evidence_present"])
        self.assertFalse(result["required_doc_snippets_present"])
        self.assertFalse(result["citation_completeness_pass"])

    def test_independent_holdout_remains_pending_blind_review(self) -> None:
        self.assertEqual(
            experiment_labels("independent_holdout"),
            ("independent_holdout_trial", "pending_independent_review"),
        )

    def test_exposed_suite_cannot_be_labeled_as_unseen_by_regression_mode(self) -> None:
        self.assertEqual(
            experiment_labels("regression"),
            ("regression_after_review", "previously_exposed_not_unseen"),
        )


if __name__ == "__main__":
    unittest.main()
