from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
import unittest

from eval.cross_source_reference_baseline import StageBReferenceBaseline


def _candidate(contract_id: str, equipment_id: str = "DEV-1") -> dict:
    return {
        "work_order_id": "WO-2026-0101",
        "equipment_id": equipment_id,
        "matched_equipment_id": equipment_id,
        "opened_at": datetime(2026, 9, 10, 9, tzinfo=timezone(timedelta(hours=8))),
        "equipment_name": "同名设备",
        "customer_id": "ORG-A",
        "mapped_contract_id": contract_id,
        "matched_contract_id": contract_id,
        "mapping_valid_from": date(2026, 9, 1),
        "mapping_valid_to": date(2026, 12, 31),
        "mapping_basis": "unverified_basis",
        "contract_customer_id": "ORG-B",
        "contract_effective_from": date(2026, 9, 1),
        "contract_effective_to": date(2026, 12, 31),
        "contract_status": "active",
    }


def _baseline(rows: list[dict]) -> StageBReferenceBaseline:
    baseline = StageBReferenceBaseline(dsn="postgresql://unused", docs_dir=None)
    baseline._read_rows = Mock(return_value=(rows, [
        {"work_order_id": "WO-2026-0101", "event_type": "accepted", "event_at": datetime(2026, 9, 10, 9, tzinfo=timezone.utc)},
        {"work_order_id": "WO-2026-0101", "event_type": "service_restored", "event_at": datetime(2026, 9, 10, 13, tzinfo=timezone.utc)},
    ]))
    baseline._retrieve_once = Mock(return_value=(
        {"service_restored": ("hours", 8)},
        {"kind": "doc", "doc_id": "HT-EVAL-001", "snippet": "repair SLA"},
        {"attempt": 1, "strategy": "primary_sla_query", "status": "required_metric_found", "candidate_count": 1},
    ))
    return baseline


def test_reference_baseline_uses_first_contract_and_does_not_apply_stage_c_checks() -> None:
    baseline = _baseline([_candidate("HT-EVAL-005"), _candidate("HT-EVAL-004")])

    result = baseline.run("工单 WO-2026-0101 是否按时完成？")

    assert result["trace"][-1]["output"]["status"] == "repair_within_limit"
    baseline._retrieve_once.assert_called_once_with("HT-EVAL-004")


def test_reference_baseline_does_not_block_negative_duration() -> None:
    baseline = _baseline([_candidate("HT-EVAL-001")])
    baseline._read_rows.return_value[1][1]["event_at"] = datetime(
        2026, 9, 10, 0, tzinfo=timezone.utc
    )

    result = baseline.run("工单 WO-2026-0101 是否按时完成？")

    assert result["trace"][-1]["output"]["status"] == "repair_within_limit"
    assert "-9小时" in result["answer"]


def test_reference_baseline_records_no_mapping_without_retrieving_docs() -> None:
    row = _candidate("HT-EVAL-001")
    row.update(mapped_contract_id=None, mapping_valid_from=None, mapping_valid_to=None)
    baseline = _baseline([row])

    result = baseline.run("工单 WO-2026-0101 是否按时完成？")

    assert result["trace"][-1]["output"]["status"] == "missing_object_mapping"
    baseline._retrieve_once.assert_not_called()
    absent = next(ref for ref in result["refs"] if "mapping_candidates=0" in ref.get("snippet", ""))
    assert absent["table"] == "cross_source_demo.contract_equipment"
    assert absent["column"] == "contract_id/equipment_id"
    assert absent["row_pk"] is None
    assert "DEV-1" in absent["snippet"]


def test_reference_baseline_handles_dangling_contract_mapping_without_type_error() -> None:
    row = _candidate("HT-MED-199")
    row.update(
        matched_contract_id=None,
        contract_customer_id=None,
        contract_effective_from=None,
        contract_effective_to=None,
        contract_status=None,
    )
    baseline = _baseline([row])

    result = baseline.run("工单 WO-2026-0101 是否按时完成？")

    assert result["trace"][-1]["output"]["status"] == "no_active_contract"
    assert "合同记录或有效期信息缺失" in result["answer"]
    baseline._retrieve_once.assert_not_called()
    missing_contract = next(
        ref for ref in result["refs"]
        if ref.get("table") == "cross_source_demo.contracts"
    )
    assert missing_contract["row_pk"] is None
    assert "HT-MED-199" in missing_contract["snippet"]
    assert "未返回匹配记录" in missing_contract["snippet"]


def test_reference_baseline_distinguishes_missing_equipment_from_missing_work_order() -> None:
    row = _candidate("HT-EVAL-001")
    row.update(matched_equipment_id=None, equipment_name=None, customer_id=None)
    baseline = _baseline([row])

    result = baseline.run("工单 WO-2026-0101 是否按时完成？")

    assert result["trace"][-1]["output"]["status"] == "missing_reference_data"
    assert "已找到工单" in result["answer"]
    assert "设备主数据中没有设备 DEV-1" in result["answer"]
    baseline._retrieve_once.assert_not_called()
    missing_equipment = next(
        ref for ref in result["refs"]
        if ref.get("table") == "cross_source_demo.equipment"
    )
    assert missing_equipment["row_pk"] is None
    assert "DEV-1" in missing_equipment["snippet"]
    assert "0 行" in missing_equipment["snippet"]


def test_reference_baseline_names_only_missing_event_with_query_evidence() -> None:
    baseline = _baseline([_candidate("HT-EVAL-001")])
    baseline._read_rows.return_value[1].pop()
    result = baseline.run("工单 WO-2026-0101 是否按时完成？")
    assert "缺少服务恢复（service_restored）事件" in result["answer"]
    assert result["trace"][-1]["output"]["missing_events"] == ["service_restored"]
    absent = next(ref for ref in result["refs"] if "event_type=service_restored 未返回记录" in ref.get("snippet", ""))
    assert absent["table"] == "cross_source_demo.work_order_events"
    assert absent["column"] == "event_type/event_at"
    assert absent["row_pk"] is None
    assert "WO-2026-0101" in absent["snippet"]


def load_tests(loader, tests, pattern):
    """让现有函数式测试也能在项目标准库 unittest 中运行，无需另装 pytest。"""
    return unittest.TestSuite(unittest.FunctionTestCase(function) for name, function in sorted(globals().items())
                              if name.startswith("test_") and callable(function))


if __name__ == "__main__":
    unittest.main()
