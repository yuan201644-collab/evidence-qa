"""隔离验证跨源演示工具的失败分支；不连接或改写演示数据库。"""

from __future__ import annotations

import unittest
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg

from app.contracts import DocRef
from app.nl2sql.normalizer import normalize
from app.tools import cross_source_demo as demo_module
from app.tools.cross_source_demo import CrossSourceDemoTool


UTC = timezone.utc
CONTRACT_ID = "HT-MNT-2026-001"
WORK_ORDER_ID = "WO-2026-0001"


def _record(
    *,
    contract_id: str = CONTRACT_ID,
    customer_id: str = "CUSTOMER-1",
    contract_customer_id: str = "CUSTOMER-1",
    mapping_basis: str = "explicit_equipment_schedule",
) -> dict[str, object]:
    return {
        "work_order_id": WORK_ORDER_ID,
        "equipment_id": "DEV-001",
        "matched_equipment_id": "DEV-001",
        "opened_at": datetime(2026, 9, 18, 9, tzinfo=UTC),
        "summary": "synthetic test work order",
        "equipment_name": "设备甲",
        "customer_id": customer_id,
        "mapped_contract_id": contract_id,
        "matched_contract_id": contract_id,
        "mapping_valid_from": date(2026, 9, 1),
        "mapping_valid_to": date(2026, 12, 31),
        "mapping_basis": mapping_basis,
        "contract_customer_id": contract_customer_id,
        "contract_effective_from": date(2026, 9, 1),
        "contract_effective_to": date(2026, 12, 31),
        "contract_status": "active",
        "contract_timezone": "Asia/Shanghai",
    }


def _events(*, negative_repair: bool = False) -> list[dict[str, object]]:
    accepted = datetime(2026, 9, 18, 9, tzinfo=UTC)
    restored = accepted - timedelta(minutes=1) if negative_repair else accepted + timedelta(hours=2)
    return [
        {"work_order_id": WORK_ORDER_ID, "event_type": "accepted", "event_at": accepted},
        {"work_order_id": WORK_ORDER_ID, "event_type": "first_response", "event_at": accepted + timedelta(minutes=5)},
        {"work_order_id": WORK_ORDER_ID, "event_type": "onsite_arrival", "event_at": accepted + timedelta(hours=1)},
        {"work_order_id": WORK_ORDER_ID, "event_type": "service_restored", "event_at": restored},
    ]


def _candidate(text: str) -> tuple[SimpleNamespace, DocRef]:
    chunk = SimpleNamespace(text=text)
    return SimpleNamespace(chunk=chunk), DocRef(
        doc_id=CONTRACT_ID,
        section_path="第三条 服务级别定义",
        snippet=text,
    )


class _SequenceRetriever:
    def __init__(self, results: list[list[tuple[SimpleNamespace, DocRef]]]) -> None:
        self.results = results
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 8, doc_id: str | None = None):
        self.queries.append(query)
        return self.results[min(len(self.queries) - 1, len(self.results) - 1)]


class CrossSourceFailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            "os.environ",
            {
                "CROSS_SOURCE_DEMO_TIME_BUDGET_MS": "5000",
                "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _tool(
        self,
        rows: list[dict[str, object]] | None = None,
        events: list[dict[str, object]] | None = None,
        retriever: _SequenceRetriever | None = None,
    ) -> CrossSourceDemoTool:
        tool = CrossSourceDemoTool(dsn="isolated-test-dsn")
        tool._load_records = lambda _work_order_id, _budget: (  # type: ignore[method-assign]
            rows if rows is not None else [_record()],
            events if events is not None else _events(),
        )
        if retriever is not None:
            tool._ensure_doc_retriever = lambda: retriever  # type: ignore[method-assign]
        return tool

    def test_sql_refs_bind_machine_labels_to_declared_columns_and_values(self) -> None:
        base = _record()
        row = _record()
        refs = CrossSourceDemoTool._sql_refs(base, [row], _events())

        def assert_labels(ref, expected: dict[str, str]) -> None:
            labels = {}
            for item in ref.snippet.rsplit(" [", 1)[1][:-1].split("; "):
                column, value = item.split("=", 1)
                labels[column] = json.loads(value)
            self.assertEqual(set(labels), set(ref.column.split("/")))
            self.assertEqual(labels, expected)

        assert_labels(refs[0], {
            "work_order_id": WORK_ORDER_ID,
            "equipment_id": "DEV-001",
            "opened_at": "2026-09-18T09:00:00+00:00",
        })
        assert_labels(refs[1], {
            "equipment_id": "DEV-001", "equipment_name": "设备甲", "customer_id": "CUSTOMER-1",
        })
        assert_labels(refs[2], {
            "equipment_id": "DEV-001", "contract_id": CONTRACT_ID,
            "valid_from": "2026-09-01", "valid_to": "2026-12-31",
            "mapping_basis": "explicit_equipment_schedule",
        })
        assert_labels(refs[3], {
            "contract_id": CONTRACT_ID, "customer_id": "CUSTOMER-1",
            "effective_from": "2026-09-01", "effective_to": "2026-12-31", "status": "active",
        })
        for ref, event in zip(refs[4:], _events()):
            assert_labels(ref, {
                "work_order_id": WORK_ORDER_ID,
                "event_type": str(event["event_type"]),
                "event_at": event["event_at"].isoformat(),
            })

    def test_missing_work_order_returns_query_derived_absence_evidence(self) -> None:
        result = self._tool(rows=[], events=[]).run({"question": "WO-2026-9999 是否有适用合同？"})

        self.assertEqual(result.trace[0].output["status"], "not_found")
        self.assertEqual(result.trace[0].output["row_count"], 0)
        self.assertEqual(result.trace[0].output["absence_checks"], [{
            "table": "cross_source_demo.work_orders",
            "filters": {"work_order_id": "WO-2026-9999"},
            "row_count": 0,
        }])
        self.assertEqual(len(result.refs), 1)
        self.assertIsNone(result.refs[0].row_pk)
        self.assertIn("work_order_id=WO-2026-9999", result.refs[0].snippet)
        self.assertIn("不能证明业务上未发生", result.data)

    def test_retries_with_targeted_query_when_first_hit_lacks_repair_sla(self) -> None:
        retriever = _SequenceRetriever([
            [_candidate("首次响应 30 分钟内。")],
            [_candidate("| 修复完成 | service_restored | 8 小时内 |")],
        ])
        result = self._tool(retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(len(retriever.queries), 2)
        self.assertEqual(result.trace[1].output["attempts"][1]["strategy"], "targeted_repair_sla_retry")
        self.assertEqual(result.trace[1].output["attempts"][1]["status"], "required_metric_found")
        self.assertEqual(result.trace[2].output["status"], "repair_within_limit")

    def test_two_column_sla_table_is_parsed_and_requested_scope_is_quoted(self) -> None:
        row = _record()
        row.update(
            equipment_id="EQ-9101",
            equipment_name="精密光谱仪（南京一号站点）",
        )
        doc = (
            "# 合同 CT-9101 服务条款\n\n## 服务时限\n"
            "| 项目 | 时限 |\n|---|---|\n"
            "| 首次响应 | 15分钟内 |\n| 现场到达 | 3小时内 |\n| 修复完成 | 5小时内 |\n\n"
            "精密光谱仪 EQ-9101 纳入校准服务范围；校准标准液批次为 CAL-26A。"
        )
        retriever = _SequenceRetriever([[_candidate(doc)]])

        result = self._tool(rows=[row], retriever=retriever).run({
            "question": (
                "WO-2026-0001：首次响应、现场到达、修复完成分别用了多久、是否达标？"
                "请核对 EQ-9101 的校准服务范围并引用原文。"
            )
        })

        self.assertEqual(result.trace[-1].output["status"], "repair_within_limit")
        self.assertIn("首次响应 5分钟", result.data)
        self.assertIn("现场到达 1小时", result.data)
        self.assertIn("修复完成 2小时", result.data)
        self.assertIn("EQ-9101 纳入校准服务范围", result.data)
        self.assertIn("合同文档原句：", result.data)
        self.assertIn("| 修复完成 | 5小时内 |", result.data)

    def test_stops_without_conclusion_when_sla_is_absent_after_attempt_limit(self) -> None:
        retriever = _SequenceRetriever([[], []])
        result = self._tool(retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(len(retriever.queries), 2)
        self.assertEqual(result.trace[1].output["status"], "insufficient_document_evidence")
        self.assertEqual(result.trace[2].output["status"], "insufficient_document_evidence")

    def test_suspended_contract_is_not_used_for_sla_even_when_dates_cover(self) -> None:
        row = _record()
        row["contract_status"] = "suspended"
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])

        result = self._tool(rows=[row], retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "no_active_contract")
        self.assertFalse(result.trace[-1].output["validity_checks"][0]["contract_active"])
        self.assertIn("状态为 suspended", result.data)
        self.assertEqual(len(retriever.queries), 1)
        self.assertEqual(result.trace[1].output["status"], "retrieved_context_only")
        self.assertFalse(result.trace[-1].output["document_used_for_sla_calculation"])
        self.assertIn("修复完成时限为 8小时（仅作条款背景）", result.data)
        self.assertIn("不据此判定达标或超时", result.data)
        contract_ref = next(ref for ref in result.refs if ref.table == "cross_source_demo.contracts")
        self.assertIn("状态字段 suspended", contract_ref.snippet)
        doc_ref = next(ref for ref in result.refs if ref.kind == "doc")
        self.assertIn("8 小时内", doc_ref.snippet)

    def test_chinese_active_contract_status_is_recognized(self) -> None:
        row = _record()
        row["contract_status"] = "生效"
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 4 小时内 |")]])

        result = self._tool(rows=[row], retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "repair_within_limit")
        self.assertIn("满足", result.data)

    def test_answer_surfaces_requested_retest_and_validity_facts_from_sources(self) -> None:
        row = _record()
        row.update(
            equipment_id="WQ-R23",
            equipment_name="出水余氯监测仪（青岚水厂）",
            mapped_contract_id="CW-SVC-731",
            matched_contract_id="CW-SVC-731",
        )
        doc = (
            "服务协议 CW-SVC-731 覆盖青岚水厂的 WQ-R23 出水余氯监测仪；"
            "服务期为 2026-01-01 至 2026-12-31。\n"
            "| 修复完成 | service_restored | 4 小时内 |\n"
            "WQ-R23 校准后应使用标准液复测两次，两次读数差不得超过 0.03 mg/L。"
        )
        retriever = _SequenceRetriever([[_candidate(doc)]])

        result = self._tool(rows=[row], retriever=retriever).run({
            "question": (
                "WO-2026-0001 是否在有效服务范围内？校准后应如何复测，"
                "允许的两次读数差是多少？另按题设假设计算工单修复时长。"
            )
        })

        self.assertIn("设备 WQ-R23（出水余氯监测仪），站点 青岚水厂", result.data)
        self.assertIn("站点 青岚水厂", result.data)
        self.assertIn("映射有效期 2026-09-01 至 2026-12-31", result.data)
        self.assertIn("映射期覆盖工单日期：是", result.data)
        self.assertIn("合同 CW-SVC-731 有效期 2026-09-01 至 2026-12-31，状态 active", result.data)
        self.assertIn("合同期覆盖工单日期：是", result.data)
        self.assertIn("WQ-R23 校准后应使用标准液复测两次", result.data)
        self.assertIn("两次读数差不得超过 0.03 mg/L", result.data)
        self.assertIn("按题设假设条件计算", result.data)
        self.assertNotIn("复测已通过", result.data)

    def test_explicit_no_sla_answer_surfaces_requested_scope_and_dates(self) -> None:
        row = _record(
            contract_id="CW-SVC-733",
            mapping_basis="explicit_equipment_schedule",
        )
        row.update(
            equipment_id="WQ-S42",
            equipment_name="南库区备用水温监测仪",
            mapped_contract_id="CW-SVC-733",
            matched_contract_id="CW-SVC-733",
            mapping_valid_from=date(2026, 7, 1),
            mapping_valid_to=date(2026, 12, 31),
            contract_effective_from=date(2026, 5, 1),
            contract_effective_to=date(2026, 12, 31),
            opened_at=datetime(2026, 8, 1, 2, tzinfo=UTC),
        )
        doc = (
            "补充单 CW-SVC-733 自 2026-07-01 起将 WQ-S42 南库区备用水温监测仪纳入服务，"
            "适用期限截至 2026-12-31。\n本补充单未约定服务恢复总时限。"
        )
        retriever = _SequenceRetriever([[_candidate(doc)]])

        result = self._tool(rows=[row], retriever=retriever).run({
            "question": "WO-2026-0001 发生于 2026-08-01，判断 WQ-S42 当时是否已纳入服务。"
        })

        self.assertIn("映射有效期 2026-07-01 至 2026-12-31", result.data)
        self.assertIn("合同 CW-SVC-733 有效期 2026-05-01 至 2026-12-31", result.data)
        self.assertIn("将 WQ-S42 南库区备用水温监测仪纳入服务", result.data)
        self.assertIn("不进行修复耗时达标比较，也不能判断是否超时", result.data)
        self.assertIn("设备属于该有效合同的适用范围", result.data)
        self.assertIn("不进行修复耗时达标比较，也不能判断是否超时", result.data)

    def test_real_contract_special_sla_overrides_standard_limit_for_matching_equipment(self) -> None:
        row = _record(contract_id="HT-MED-107")
        row.update(
            equipment_id="DEV-MED-307",
            equipment_name="超声影像一体机",
            opened_at=datetime(2026, 10, 16, 8, 30, tzinfo=UTC),
        )
        accepted = datetime(2026, 10, 16, 8, 30, tzinfo=UTC)
        events = [
            {"work_order_id": WORK_ORDER_ID, "event_type": "accepted", "event_at": accepted},
            {
                "work_order_id": WORK_ORDER_ID,
                "event_type": "service_restored",
                "event_at": accepted + timedelta(hours=5),
            },
        ]
        docs = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "2026Q4_v0.3" / "data" / "documents"
        with patch.dict("os.environ", {"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs)}):
            result = self._tool(rows=[row], events=events).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "repair_over_limit")
        self.assertIn("要求不超过 4小时，超时 1小时", result.data)
        doc_ref = next(ref for ref in result.refs if ref.kind == "doc")
        self.assertIn("| 修复完成 | service_restored | 6小时内 |", doc_ref.snippet)
        self.assertIn("| 修复完成 | service_restored | 4小时内 |", doc_ref.snippet)
        self.assertIn("特别约定优先于上列标准服务时限", doc_ref.snippet)

    def test_customer_mismatch_stops_before_document_retrieval(self) -> None:
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])
        result = self._tool(
            rows=[_record(contract_customer_id="OTHER-CUSTOMER")], retriever=retriever
        ).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "customer_scope_conflict")
        self.assertEqual(retriever.queries, [])
        self.assertIn("不一致", result.data)
        equipment_ref = next(
            ref for ref in result.refs if ref.table == "cross_source_demo.equipment"
        )
        self.assertIn("customer_id", equipment_ref.column)
        self.assertIn("CUSTOMER-1", equipment_ref.snippet)
        self.assertEqual(result.trace[-1].input["equipment_customer_id"], "CUSTOMER-1")
        self.assertEqual(result.trace[-1].input["contract_customer_id"], "OTHER-CUSTOMER")

    def test_explicit_missing_repair_limit_is_distinguished_from_retrieval_miss(self) -> None:
        clause = (
            "合同说明与一般服务安排。" * 16
            + "本合同约束首次响应和现场到达，但不约定服务恢复总时限。"
            "不得依据响应或到场时限推断修复完成时限。"
        )
        retriever = _SequenceRetriever([[_candidate(clause)]])
        result = self._tool(retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[1].output["status"], "retrieved")
        self.assertEqual(result.trace[1].output["repair_limit_status"], "explicitly_not_defined")
        self.assertEqual(result.trace[-1].output["status"], "insufficient_contract_evidence")
        self.assertIn("明确未约定服务恢复总时限", result.data)
        doc_refs = [ref for ref in result.refs if isinstance(ref, DocRef)]
        self.assertEqual(len(doc_refs), 1)
        self.assertIn("不约定服务恢复总时限", doc_refs[0].snippet)

    def test_explicit_omission_ref_focuses_text_and_adjusts_character_offsets(self) -> None:
        text = "服务条款背景说明。" * 24 + "本合同未约定服务恢复总时限。不得由其他时限推断。"
        chunk = SimpleNamespace(text=text, char_start=300, char_end=300 + len(text))
        ref = DocRef(
            doc_id=CONTRACT_ID,
            page=1,
            section_path="服务条款",
            snippet=text[:200],
            char_start=300,
            char_end=300 + len(text),
        )

        focused = CrossSourceDemoTool._repair_limit_ref(chunk, ref)

        self.assertIn("未约定服务恢复总时限", focused.snippet)
        self.assertEqual(focused.char_start, 300 + text.index("未约定服务恢复总时限") - 100)
        self.assertEqual(focused.char_end, 300 + len(text))
        self.assertEqual(focused.char_end - focused.char_start, len(focused.snippet))

    def test_inactive_contract_context_reports_explicit_sla_omission(self) -> None:
        row = _record()
        row["contract_status"] = "suspended"
        clause = "合同没有其他约定。本合同未约定服务恢复总时限。"
        retriever = _SequenceRetriever([[_candidate(clause)]])

        result = self._tool(rows=[row], retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "no_active_contract")
        self.assertEqual(
            result.trace[1].output["attempts"][0]["status"],
            "required_metric_absence_explicit",
        )
        self.assertIn("明确未约定服务恢复总时限", result.data)
        self.assertNotIn("未取得可引用的修复时限条款", result.data)
        doc_ref = next(ref for ref in result.refs if ref.kind == "doc")
        self.assertIn("未约定服务恢复总时限", doc_ref.snippet)

    def test_inactive_contract_answer_reports_both_windows_and_status_when_asked(self) -> None:
        row = _record()
        row["contract_status"] = "suspended"
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | 8 小时内 |")]])

        result = self._tool(rows=[row], retriever=retriever).run({
            "question": (
                "WO-2026-0001 的映射期和合同期是否覆盖工单日期？"
                "请说明合同状态及其适用性。"
            )
        })

        self.assertIn("映射期覆盖工单日期=是", result.data)
        self.assertIn("合同期覆盖工单日期=是", result.data)
        self.assertIn("合同状态=suspended", result.data)
        self.assertIn("已核实同时满足两种日期窗口且状态有效：否", result.data)

    def test_unapproved_mapping_basis_stops_before_document_retrieval(self) -> None:
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])
        result = self._tool(
            rows=[_record(mapping_basis="same_equipment_name")], retriever=retriever
        ).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "mapping_basis_not_allowed")
        self.assertEqual(retriever.queries, [])
        self.assertIn("explicit_equipment_schedule", result.data)
        self.assertIn("signed_asset_register", result.data)
        self.assertEqual(
            result.trace[-1].output["approved_mapping_bases"],
            [
                "attached_asset_schedule",
                "explicit_equipment_schedule",
                "signed_acceptance_record",
                "signed_asset_register",
            ],
        )

    def test_multiple_active_contracts_stop_as_ambiguous(self) -> None:
        second = _record(contract_id="HT-MNT-2026-OTHER")
        result = self._tool(rows=[_record(), second]).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "ambiguous_active_contracts")
        self.assertEqual(result.trace[-1].output["stop_reason_code"], "ambiguous_active_contracts")
        self.assertIn("多个有效合同", result.data)

    def test_mapping_and_contract_validity_windows_are_reported_separately(self) -> None:
        work_date = date(2026, 10, 9)
        scenarios = (
            (
                "mapping expired, contract active",
                {"mapping_valid_to": date(2026, 9, 20)},
                "设备—合同映射有效期",
                "合同有效期",
                False,
                True,
            ),
            (
                "mapping active, contract not yet effective",
                {"contract_effective_from": date(2026, 10, 15)},
                "合同有效期",
                "设备—合同映射有效期",
                True,
                False,
            ),
            (
                "both windows start after the work order",
                {
                    "mapping_valid_from": date(2026, 10, 15),
                    "contract_effective_from": date(2026, 10, 15),
                },
                "设备—合同映射有效期",
                None,
                False,
                False,
            ),
        )
        for label, changes, expected_reason, not_failed_reason, mapping_covers, contract_covers in scenarios:
            with self.subTest(label=label):
                row = _record()
                row.update(changes)
                row["opened_at"] = datetime(2026, 10, 9, 10, tzinfo=UTC)
                retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])
                result = self._tool(rows=[row], retriever=retriever).run({"question": WORK_ORDER_ID})

                self.assertEqual(result.trace[-1].output["status"], "no_active_contract")
                self.assertIn(expected_reason, result.data)
                if not_failed_reason:
                    self.assertNotIn(not_failed_reason, result.data)
                checks = result.trace[-1].output["validity_checks"]
                self.assertEqual(len(checks), 1)
                self.assertEqual(checks[0]["mapping_covers_work_date"], mapping_covers)
                self.assertEqual(checks[0]["contract_covers_work_date"], contract_covers)
                self.assertEqual(retriever.queries, [])

    def test_expired_mapping_and_contract_receive_direct_yes_no_answers(self) -> None:
        row = _record()
        row.update(
            equipment_id="WQ-P31",
            equipment_name="泵站振动监测仪（东岭泵站）",
            mapped_contract_id="CW-SVC-732",
            matched_contract_id="CW-SVC-732",
            mapping_valid_from=date(2026, 3, 1),
            mapping_valid_to=date(2026, 9, 30),
            contract_effective_from=date(2026, 3, 1),
            contract_effective_to=date(2026, 9, 30),
            contract_status="expired",
            opened_at=datetime(2026, 10, 2, 1, 30, tzinfo=UTC),
        )

        result = self._tool(rows=[row]).run({
            "question": (
                "WO-2026-0001 的设备在工单日期是否同时处于 CW-SVC-732 的"
                "设备映射和合同有效期内？"
            )
        })

        self.assertIn("映射期覆盖工单日期=否", result.data)
        self.assertIn("合同期覆盖工单日期=否", result.data)
        self.assertIn("合同状态=expired", result.data)
        self.assertIn("适用性直接结论：合同 CW-SVC-732 是否已核实同时满足两种日期窗口且状态有效：否", result.data)

    def test_normalized_date_range_question_reports_mapping_and_contract_separately(self) -> None:
        row = _record(contract_id="CT-733")
        row.update(
            equipment_id="EQ-733",
            equipment_name="镜湖温控柜（藻池站）",
            mapping_valid_from=date(2026, 1, 1),
            mapping_valid_to=date(2026, 12, 31),
            contract_effective_from=date(2026, 1, 1),
            contract_effective_to=date(2026, 6, 30),
            opened_at=datetime(2026, 7, 8, 2, 10, tzinfo=UTC),
        )
        question = (
            "核验 WO-2026-7803：设备排期映射和 CT-733 合同主表的日期范围分别怎样？"
            "工单当天是否仍在合同期内？"
        )
        normalized = normalize(question).normalized

        result = self._tool(rows=[row]).run({"question": normalized})

        self.assertIn("合同主表", normalized)
        self.assertIn("合同期内", normalized)
        self.assertIn("工单日期 2026-07-08", result.data)
        self.assertIn("映射有效期 2026-01-01 至 2026-12-31", result.data)
        self.assertIn("映射期覆盖工单日期=是", result.data)
        self.assertIn("合同期覆盖工单日期=否", result.data)
        self.assertIn("合同有效期2026-01-01 至 2026-06-30不覆盖", result.data)

    def test_missing_accepted_question_quotes_contract_and_preserves_boundary_request(self) -> None:
        row = _record(contract_id="CT-735")
        row.update(equipment_id="EQ-735", equipment_name="晨汐压力记录仪（西渠站）")
        events = [event for event in _events() if event["event_type"] != "accepted"]
        doc = (
            "# CT-735 服务条款\n| 服务节点 | 适用范围 | 时限 |\n"
            "| 首次响应 | 维护对象 | 25分钟内 |\n"
            "| 现场到达 | 维护对象 | 4小时内 |\n"
            "| 修复完成 | 维护对象 | 6小时内 |"
        )
        retriever = _SequenceRetriever([[_candidate(doc)]])
        question = (
            "WO-2026-7805 的记录里没有 accepted 时间。还能否从服务恢复记录判定修复是否达标？"
            "请说明数据库缺项能支持的结论边界，并引合同原句。"
        )
        normalized = normalize(question).normalized

        result = self._tool(rows=[row], events=events, retriever=retriever).run(
            {"question": normalized}
        )

        self.assertIn("说明数据库缺项", normalized)
        self.assertIn("合同原句", normalized)
        self.assertIn("不能证明业务事件没有发生", result.data)
        self.assertIn("合同文档原句：", result.data)
        self.assertIn("| 修复完成 | 维护对象 | 6小时内 |", result.data)

    def test_missing_restored_question_reports_asked_intervals_and_sla_quote(self) -> None:
        row = _record(contract_id="CT-736")
        row.update(equipment_id="EQ-736", equipment_name="杉原电流监测盒（七号库）")
        events = [event for event in _events() if event["event_type"] != "service_restored"]
        doc = (
            "# CT-736 服务级别\n| 服务节点 | 适用范围 | 时限 |\n"
            "| 首次响应 | 指定设备 | 15 分钟内 |\n"
            "| 现场到达 | 指定设备 | 2 小时内 |\n"
            "| 修复完成 | 指定设备 | 5 小时内 |"
        )
        retriever = _SequenceRetriever([[_candidate(doc)]])
        question = (
            "对于 WO-2026-0001，请汇报已有的响应和到场间隔；没有服务恢复时间时，"
            "修复时限应如何表述？引用适用合同中的时限原句。"
        )
        normalized = normalize(question).normalized

        result = self._tool(rows=[row], events=events, retriever=retriever).run(
            {"question": normalized}
        )

        self.assertIn("受理至首次响应的记录间隔为 5分钟", result.data)
        self.assertIn("受理至现场到达的记录间隔为 1小时", result.data)
        self.assertIn("合同文档原句：", result.data)
        self.assertIn("| 首次响应 | 指定设备 | 15 分钟内 |", result.data)
        self.assertIn("| 现场到达 | 指定设备 | 2 小时内 |", result.data)
        self.assertIn("| 修复完成 | 指定设备 | 5 小时内 |", result.data)

    def test_validity_date_uses_contract_timezone_across_utc_midnight(self) -> None:
        row = _record()
        row.update(
            mapping_valid_from=date(2026, 8, 15),
            mapping_valid_to=date(2026, 12, 31),
            contract_effective_from=date(2026, 8, 15),
            contract_effective_to=date(2026, 12, 31),
            opened_at=datetime(2026, 8, 14, 23, 40, tzinfo=UTC),
            contract_timezone="Asia/Shanghai",
        )
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])

        result = self._tool(rows=[row], retriever=retriever).run({
            "question": "WO-2026-0001 于 2026-08-15 的映射和合同有效期是否覆盖工单日期？"
        })

        self.assertEqual(result.trace[-1].output["status"], "repair_within_limit")
        self.assertIn("工单日期 2026-08-15", result.data)
        self.assertIn("映射期覆盖工单日期：是", result.data)
        self.assertIn("合同期覆盖工单日期：是", result.data)

    def test_validity_date_accepts_numeric_contract_utc_offset(self) -> None:
        opened_at = datetime(2026, 8, 14, 23, 40, tzinfo=UTC)

        local_date = CrossSourceDemoTool._work_order_date({
            "opened_at": opened_at,
            "contract_timezone": "+08:00",
        })

        self.assertEqual(local_date, date(2026, 8, 15))

    def test_dangling_contract_mapping_is_not_misreported_as_expired_contract(self) -> None:
        row = _record(contract_id="HT-MED-199")
        row.update(
            matched_contract_id=None,
            contract_customer_id=None,
            contract_effective_from=None,
            contract_effective_to=None,
            contract_status=None,
        )

        result = self._tool(rows=[row]).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "no_active_contract")
        self.assertEqual(result.trace[-1].output["stop_reason_code"], "mapping_unresolved")
        self.assertIn({
            "table": "cross_source_demo.contracts",
            "filters": {"contract_id": "HT-MED-199"},
            "row_count": 0,
        }, result.trace[0].output["absence_checks"])
        self.assertIn("合同主表未找到合同 HT-MED-199 的匹配记录", result.data)
        self.assertNotIn("合同有效期未提供 至 未提供不覆盖", result.data)
        check = result.trace[-1].output["validity_checks"][0]
        self.assertFalse(check["contract_record_found"])
        self.assertTrue(check["mapping_covers_work_date"])
        contract_ref = next(
            ref for ref in result.refs if ref.table == "cross_source_demo.contracts"
        )
        self.assertIsNone(contract_ref.row_pk)
        self.assertIn("HT-MED-199", contract_ref.snippet)
        self.assertIn("未返回匹配记录", contract_ref.snippet)

    def test_requested_validity_does_not_call_missing_contract_window_expired(self) -> None:
        row = _record(contract_id="CT-MISSING")
        row.update(
            matched_contract_id=None,
            contract_customer_id=None,
            contract_effective_from=None,
            contract_effective_to=None,
            contract_status=None,
        )

        result = self._tool(rows=[row]).run({
            "question": (
                "WO-2026-0001 的映射期与合同期分别是否覆盖工单日期？"
                "请说明合同状态。"
            )
        })

        self.assertIn("映射期覆盖工单日期=是", result.data)
        self.assertIn("合同期覆盖工单日期=无法核实（合同主记录缺失）", result.data)
        self.assertIn("合同状态=未提供", result.data)
        self.assertIn("同时满足两种日期窗口且状态有效：无法确认", result.data)
        self.assertNotIn("合同期覆盖工单日期=否", result.data)

    def test_missing_accepted_event_explains_record_boundary_interval_and_doc_quote(self) -> None:
        accepted = datetime(2026, 9, 18, 9, tzinfo=UTC)
        events = [
            {
                "work_order_id": WORK_ORDER_ID,
                "event_type": "first_response",
                "event_at": accepted + timedelta(hours=1),
            },
            {
                "work_order_id": WORK_ORDER_ID,
                "event_type": "service_restored",
                "event_at": accepted + timedelta(hours=3),
            },
        ]
        row = _record()
        row["equipment_id"] = "EQ-9109"
        row["matched_equipment_id"] = "EQ-9109"
        doc = (
            "# 合同条款\n| 项目 | 时限 |\n|---|---|\n"
            "| 首次响应 | 30分钟内 |\n| 现场到达 | 4小时内 |\n| 修复完成 | 6小时内 |\n\n"
            "微生物鉴定系统 EQ-9109 纳入维护。服务范围按设备编号核对。"
        )
        retriever = _SequenceRetriever([[_candidate(doc)]])

        result = self._tool(rows=[row], events=events, retriever=retriever).run({
            "question": (
                "WO-2026-0001 的服务恢复耗时和达标情况是什么？数据库中是否有受理事件？"
                "请说明缺失记录能证明什么、不能证明什么，并列出相关文档原句。"
            )
        })

        self.assertEqual(result.trace[-1].output["status"], "missing_required_event")
        self.assertIn("不能证明业务事件没有发生", result.data)
        self.assertIn("间隔为 2小时", result.data)
        self.assertIn("不是以 accepted 为起点的合同修复耗时", result.data)
        self.assertIn("合同文档原句：", result.data)
        self.assertIn("| 修复完成 | 6小时内 |", result.data)
        self.assertTrue(any("event_type=accepted 未返回记录" in ref.snippet for ref in result.refs if ref.kind == "sql"))

    def test_missing_equipment_master_is_distinguished_from_missing_work_order(self) -> None:
        row = _record()
        row.update(matched_equipment_id=None, equipment_name=None, customer_id=None)

        result = self._tool(rows=[row]).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "missing_reference_data")
        self.assertEqual(result.trace[-1].output["stop_reason_code"], "equipment_master_missing")
        self.assertIn({
            "table": "cross_source_demo.equipment",
            "filters": {"equipment_id": "DEV-001"},
            "row_count": 0,
        }, result.trace[0].output["absence_checks"])
        self.assertIn("已找到工单", result.data)
        self.assertEqual(result.trace[0].output["event_types"], [
            "accepted", "first_response", "onsite_arrival", "service_restored",
        ])
        self.assertEqual(
            {ref.table for ref in result.refs},
            {"cross_source_demo.work_orders", "cross_source_demo.equipment"},
        )
        missing_equipment = next(
            ref for ref in result.refs if ref.table == "cross_source_demo.equipment"
        )
        self.assertIsNone(missing_equipment.row_pk)
        self.assertIn("DEV-001", missing_equipment.snippet)
        self.assertIn("0 行", missing_equipment.snippet)

    def test_missing_mapping_query_has_executor_absence_metadata(self) -> None:
        row = _record()
        row["mapped_contract_id"] = None
        row["matched_contract_id"] = None
        row["mapping_basis"] = None

        result = self._tool(rows=[row]).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[-1].output["status"], "missing_object_mapping")
        self.assertIn({
            "table": "cross_source_demo.contract_equipment",
            "filters": {"equipment_id": "DEV-001"},
            "row_count": 0,
        }, result.trace[0].output["absence_checks"])
        self.assertTrue(any(
            ref.table == "cross_source_demo.contract_equipment"
            and ref.row_pk is None
            and "按设备编号 DEV-001 查询合同设备映射未返回记录" in ref.snippet
            and "equipment_id=DEV-001" in ref.snippet
            and "mapping_candidates=0" in ref.snippet
            for ref in result.refs
        ))

    def test_negative_repair_duration_is_not_reported_as_a_pass(self) -> None:
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])
        result = self._tool(events=_events(negative_repair=True), retriever=retriever).run(
            {"question": WORK_ORDER_ID}
        )

        self.assertEqual(result.trace[-1].output["status"], "invalid_event_order")
        self.assertEqual(result.trace[-1].output["invalid_metrics"][0]["reason"], "end_event_before_start_event")
        self.assertIn("时间顺序异常", result.data)

    def test_missing_events_have_precise_names_and_negative_query_evidence(self) -> None:
        for missing in (["service_restored"], ["accepted"], ["accepted", "service_restored"]):
            with self.subTest(missing=missing):
                retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])
                events = [event for event in _events() if event["event_type"] not in missing]
                result = self._tool(events=events, retriever=retriever).run({"question": WORK_ORDER_ID})
                self.assertEqual(result.trace[-1].output["status"], "missing_required_event")
                self.assertEqual(result.trace[-1].output["missing_events"], missing)
                if "service_restored" in missing:
                    self.assertIn(
                        "不能证明服务在现实中尚未恢复", result.data
                    )
                if "accepted" in missing:
                    self.assertIn(
                        "不能证明业务事件没有发生", result.data
                    )
                for name in missing:
                    self.assertIn("（" + name + "）", result.data)
                    absent = next(ref for ref in result.refs if "event_type=" + name + " 未返回记录" in ref.snippet)
                    self.assertEqual(absent.table, "cross_source_demo.work_order_events")
                    self.assertEqual(absent.column, "event_type/event_at")
                    self.assertIsNone(absent.row_pk)
                    self.assertIn(WORK_ORDER_ID, absent.snippet)
                self.assertNotIn("缺少受理或服务恢复", result.data)
                encode = lambda ref: ref.model_dump_json()
                self.assertEqual({encode(ref) for ref in result.refs}, {
                    encode(ref) for step in result.trace for ref in (step.hit or [])
                })

    def test_successful_query_with_no_events_records_both_missing_endpoints(self) -> None:
        retriever = _SequenceRetriever([[_candidate("| 修复完成 | service_restored | 8 小时内 |")]])
        result = self._tool(events=[], retriever=retriever).run({"question": WORK_ORDER_ID})
        self.assertEqual(result.trace[-1].output["missing_events"], ["accepted", "service_restored"])
        absent = [ref for ref in result.refs if "event_types=[]" in ref.snippet]
        self.assertEqual(len(absent), 1)
        from app.orchestrator import Orchestrator
        from app.tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(self._tool(events=[], retriever=retriever))
        response = Orchestrator(registry).answer(WORK_ORDER_ID, "missing-both-unit")
        encode = lambda ref: ref.model_dump_json()
        self.assertEqual({encode(ref) for ref in response.refs}, {
            encode(ref) for step in response.trace for ref in (step.hit or [])
        })

    def test_budget_exhaustion_during_second_retrieval_is_recorded(self) -> None:
        retriever = _SequenceRetriever([[], [_candidate("| 修复完成 | service_restored | 8 小时内 |")]])

        class ExhaustDuringSecondRetrieval:
            calls = 0

            def __init__(self, _timeout_ms: int) -> None:
                pass

            def remaining_ms(self) -> int:
                self.calls += 1
                # _retrieve_contract_limits: before loop, before attempt 1,
                # after attempt 1, before attempt 2, then expire after its retrieval.
                if self.calls == 6:
                    raise demo_module._BudgetExceeded("test budget exhausted")
                return 500

        with patch.object(demo_module, "_RunBudget", ExhaustDuringSecondRetrieval):
            result = self._tool(retriever=retriever).run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[1].output["status"], "budget_exhausted")
        self.assertEqual(
            result.trace[1].output["attempts"][-1]["status"],
            "budget_exhausted_during_retrieval",
        )
        self.assertEqual(result.trace[-1].output["status"], "budget_exhausted")
        self.assertIn("不判断是否达标", result.data)

    def test_database_connection_failure_stops_without_claiming_no_matching_record(self) -> None:
        connect = MagicMock(side_effect=psycopg.OperationalError("synthetic connection failure"))
        tool = CrossSourceDemoTool(dsn="isolated-test-dsn")

        with patch("psycopg.connect", connect):
            result = tool.run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[0].output["status"], "failed")
        self.assertEqual(result.trace[0].output["error_type"], "OperationalError")
        self.assertIn("数据库查询失败", result.data)
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 5)

    def test_database_statement_timeout_is_reported_as_budget_exhausted(self) -> None:
        class ReadOnlyCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, _params=None):
                if query == demo_module._WORK_ORDER_SQL:
                    raise psycopg.errors.QueryCanceled("synthetic statement timeout")

            @staticmethod
            def fetchone():
                return {"database_name": "cross_source_demo"}

        class ReadOnlyConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def cursor(row_factory=None):
                return ReadOnlyCursor()

        tool = CrossSourceDemoTool(dsn="isolated-test-dsn")
        with patch("psycopg.connect", return_value=ReadOnlyConnection()):
            result = tool.run({"question": WORK_ORDER_ID})

        self.assertEqual(result.trace[0].output["status"], "budget_exhausted")
        self.assertEqual(result.trace[0].output["error_type"], "QueryCanceled")
        self.assertIn("达到总时限", result.data)


if __name__ == "__main__":
    unittest.main()
