from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.tools.cross_source_demo import CrossSourceDemoTool


def _ready_work_order_row():
    offset = timezone(timedelta(hours=8))
    return {
        "work_order_id": "WO-2026-0001",
        "equipment_id": "DEV-001",
        "equipment_name": "智算节点（机房A）",
        "opened_at": datetime(2026, 9, 18, 9, tzinfo=offset),
        "contract_timezone": "+08:00",
        "matched_equipment_id": "DEV-001",
        "customer_id": "ORG-001",
        "mapped_contract_id": "HT-MNT-2026-001",
        "mapping_valid_from": date(2026, 9, 1),
        "mapping_valid_to": date(2026, 12, 31),
        "mapping_basis": "explicit_equipment_schedule",
        "matched_contract_id": "HT-MNT-2026-001",
        "contract_customer_id": "ORG-001",
        "contract_effective_from": date(2026, 9, 1),
        "contract_effective_to": date(2026, 12, 31),
        "contract_status": "active",
    }


def test_demo_readiness_requires_work_order_events_mapping_and_repair_clause(
    monkeypatch, tmp_path
):
    (tmp_path / "HT-MNT-2026-001.md").write_text(
        "| 修复完成 | 工单受理后 | 8 小时内 |\n", encoding="utf-8"
    )
    monkeypatch.setenv("CROSS_SOURCE_DEMO_DOCS_DIR", str(tmp_path))
    tool = CrossSourceDemoTool(dsn="configured-but-stubbed")
    monkeypatch.setattr(
        tool,
        "_load_records",
        lambda _work_order_id, _budget: (
            [_ready_work_order_row()],
            [
                {"event_type": "accepted", "event_at": datetime(2026, 9, 18, 9, tzinfo=timezone(timedelta(hours=8)))},
                {"event_type": "service_restored", "event_at": datetime(2026, 9, 18, 16, 40, tzinfo=timezone(timedelta(hours=8)))},
            ],
        ),
    )

    class Retriever:
        def retrieve(self, _query, *, top_k, doc_id):
            assert top_k == 8
            assert doc_id == "HT-MNT-2026-001"
            chunk = SimpleNamespace(text="| 修复完成 | 工单受理后 | 8 小时内 |")
            return [(SimpleNamespace(chunk=chunk), object())]

    monkeypatch.setattr(tool, "_ensure_doc_retriever", lambda: Retriever())

    assert tool.probe_demo_case_readiness() is True


def test_demo_readiness_fails_closed_when_required_event_or_contract_mapping_is_missing(
    monkeypatch, tmp_path
):
    (tmp_path / "HT-MNT-2026-001.md").write_text(
        "| 修复完成 | 工单受理后 | 8 小时内 |\n", encoding="utf-8"
    )
    monkeypatch.setenv("CROSS_SOURCE_DEMO_DOCS_DIR", str(tmp_path))
    tool = CrossSourceDemoTool(dsn="configured-but-stubbed")
    monkeypatch.setattr(
        tool,
        "_load_records",
        lambda _work_order_id, _budget: (
            [{**_ready_work_order_row(), "mapped_contract_id": "HT-MNT-OTHER"}],
            [{"event_type": "accepted", "event_at": datetime(2026, 9, 18, 9, tzinfo=timezone(timedelta(hours=8)))}],
        ),
    )
    monkeypatch.setattr(
        tool,
        "_ensure_doc_retriever",
        lambda: (_ for _ in ()).throw(AssertionError("should stop before document retrieval")),
    )

    assert tool.probe_demo_case_readiness() is False


def test_work_order_date_accepts_contract_utc_offset_string():
    row = _ready_work_order_row()

    assert CrossSourceDemoTool._work_order_date(row) == date(2026, 9, 18)
