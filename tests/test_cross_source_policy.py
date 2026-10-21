from __future__ import annotations

import json

from app.tools.cross_source_policy import (
    allowed_mapping_bases,
    load_mapping_basis_policy,
    mapping_basis_status,
)


def test_mapping_basis_policy_is_versioned_and_three_state() -> None:
    policy = load_mapping_basis_policy()
    assert policy["version"]
    assert policy["source"]["document"].endswith("跨源业务规则与比较流程提案_v0.1.md")
    assert "explicit_equipment_schedule" in allowed_mapping_bases()
    assert mapping_basis_status("explicit_equipment_schedule") == "allowed"
    assert mapping_basis_status("equipment_name_guess") == "undefined"
    assert mapping_basis_status("unlisted_future_basis") == "undefined"


def test_runtime_mapping_basis_policy_is_selected_by_environment(monkeypatch, tmp_path) -> None:
    policy_path = tmp_path / "mapping_basis_policy.json"
    policy_path.write_text(json.dumps({
        "policy_id": "runtime_test_policy",
        "version": "test-v1",
        "source": {"kind": "frozen_runtime_protocol"},
        "allowed": ["executed_asset_schedule"],
        "denied": ["dispatch_note"],
    }), encoding="utf-8")
    monkeypatch.setenv("CROSS_SOURCE_MAPPING_POLICY_PATH", str(policy_path))

    assert allowed_mapping_bases() == frozenset({"executed_asset_schedule"})
    assert mapping_basis_status("executed_asset_schedule") == "allowed"
    assert mapping_basis_status("dispatch_note") == "denied"
    assert mapping_basis_status("explicit_equipment_schedule") == "undefined"


def test_structured_runtime_mapping_bases_are_normalized(monkeypatch, tmp_path) -> None:
    policy_path = tmp_path / "mapping_basis_policy.json"
    policy_path.write_text(json.dumps({
        "policy_id": "structured_runtime_policy",
        "version": "test-v2",
        "source": "frozen candidate package",
        "allowed": [{
            "basis": "deployment_manifest_signed",
            "meaning": "signed deployment manifest",
        }],
        "denied": [{
            "basis": "field_note",
            "meaning": "uncontrolled field note",
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("CROSS_SOURCE_MAPPING_POLICY_PATH", str(policy_path))

    policy = load_mapping_basis_policy()
    assert policy["allowed"] == ["deployment_manifest_signed"]
    assert policy["denied"] == ["field_note"]
    assert mapping_basis_status("deployment_manifest_signed") == "allowed"
    assert mapping_basis_status("field_note") == "denied"

