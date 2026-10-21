import json
import hashlib
import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

import prepare_architecture_blind_review as blind  # noqa: E402
from run_architecture_comparison import sha256_file  # noqa: E402


def _write_run(run_dir: Path) -> str:
    rows = []
    for arm in ("old_workflow", "new_fact_verification"):
        rows.append({
            "case_id": "CASE-01", "arm": arm, "question": "问工单是否超时？",
            "work_order_id": "WO-1", "status": "completed",
            "response": {
                "answer": "工单 WO-1 的结论为无法判断。",
                "refs": [{"kind": "sql", "table": "work_orders", "snippet": "WO-1"}],
                "trace": [{"tool": arm, "output": "secret trace"}],
            },
        })
    raw = run_dir / "raw_responses.jsonl"
    raw.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    manifest = {
        "status": "sealed", "expected_record_count": 2, "record_count": 2,
        "runtime_error_count": 0, "postrun_integrity_issues": [],
        "raw_responses_sha256": sha256_file(raw),
    }
    seal = run_dir / "sealed_manifest.json"
    seal.write_text(json.dumps(manifest), encoding="utf-8")
    return sha256_file(seal)


def test_blind_review_requires_external_seal_and_removes_trace_group_metadata(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seal_digest = _write_run(run_dir)
    sealed, records = blind.load_sealed_run(run_dir, seal_digest)
    packet, key = blind.build_review_packet(records, sealed)
    assert "secret trace" not in packet
    assert "new_fact_verification" not in packet
    assert "old_workflow" not in packet
    assert "过度拒答" in packet
    assert "匿名组别标签：A" in packet or "匿名组别标签：B" in packet
    assert key["review_packet_sha256"] == hashlib.sha256(packet.encode("utf-8")).hexdigest()
    assert "完成全部判分后填写" in packet
    assert len(key["candidate_mapping"]) == 2
    assert len({item["question_id"] for item in key["candidate_mapping"]}) == 2
    assert "question_mapping" not in key
    ratings_text, blindness_text = blind.build_rating_templates(key)
    rating_rows = [json.loads(line) for line in ratings_text.splitlines()]
    assert {row["candidate_id"] for row in rating_rows} == {
        row["candidate_id"] for row in key["candidate_mapping"]
    }
    assert all(row["correctness"] == "" and row["evidence"] == "" for row in rating_rows)
    assert json.loads(blindness_text) == {
        "packet_sha256": key["review_packet_sha256"], "confidence": "", "label_mapping_guess": ""
    }
    assert {row["arm"] for row in key["candidate_mapping"]} == {
        "old_workflow", "new_fact_verification"
    }

    with pytest.raises(RuntimeError, match="外部锚点"):
        blind.load_sealed_run(run_dir, "0" * 64)


def test_blind_review_rejects_modified_raw_output(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seal_digest = _write_run(run_dir)
    with (run_dir / "raw_responses.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(RuntimeError, match="原始回答哈希"):
        blind.load_sealed_run(run_dir, seal_digest)
