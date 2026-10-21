import json
import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

import score_architecture_blind_review as scorer  # noqa: E402


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")


def _key(prefix: str) -> dict:
    old_label, new_label = ("A", "B") if prefix == "A" else ("B", "A")
    return {"candidate_mapping": [
        {"candidate_id": prefix + "1", "case_id": "C1", "arm": "old_workflow", "blind_label": old_label},
        {"candidate_id": prefix + "2", "case_id": "C1", "arm": "new_fact_verification", "blind_label": new_label},
        {"candidate_id": prefix + "3", "case_id": "C2", "arm": "old_workflow", "blind_label": old_label},
        {"candidate_id": prefix + "4", "case_id": "C2", "arm": "new_fact_verification", "blind_label": new_label},
    ], "review_packet_sha256": ("a" if prefix == "A" else "b") * 64,
        "arm_label_mapping": {old_label: "old_workflow", new_label: "new_fact_verification"}}


def _rating(candidate_id: str, correctness: str, evidence: str,
            over_refusal: str = "否", packet_sha256: str | None = None) -> dict:
    packet_sha256 = packet_sha256 or (("a" if candidate_id.startswith("A") else "b") * 64)
    return {"candidate_id": candidate_id, "packet_sha256": packet_sha256, "correctness": correctness,
            "evidence": evidence, "over_refusal": over_refusal, "notes": "核对记录"}


def test_human_review_scoring_uses_predefined_primary_and_adjudication(tmp_path):
    key1_path, key2_path = tmp_path / "key1.json", tmp_path / "key2.json"
    _write_json(key1_path, _key("A"))
    _write_json(key2_path, _key("B"))
    r1_path, r2_path = tmp_path / "r1.jsonl", tmp_path / "r2.jsonl"
    _write_jsonl(r1_path, [
        _rating("A1", "正确", "充分"), _rating("A2", "正确", "充分"),
        _rating("A3", "错误", "不充分"), _rating("A4", "正确", "部分充分"),
    ])
    _write_jsonl(r2_path, [
        _rating("B1", "部分正确", "充分"), _rating("B2", "正确", "充分"),
        _rating("B3", "错误", "不充分"), _rating("B4", "正确", "部分充分"),
    ])
    blind1, blind2 = tmp_path / "blind1.json", tmp_path / "blind2.json"
    _write_json(blind1, {"packet_sha256": "a" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    _write_json(blind2, {"packet_sha256": "b" * 64, "label_mapping_guess": "A_old_B_new", "confidence": "中"})
    adjudication = tmp_path / "adjudication.jsonl"
    template = scorer.build_adjudication_template(r1_path, key1_path, r2_path, key2_path)
    template_rows = [json.loads(line) for line in template.splitlines()]
    assert [row["candidate_id"] for row in template_rows] == ["A1"]
    template_rows[0].update(correctness="正确", evidence="充分", over_refusal="否", notes="裁决")
    _write_jsonl(adjudication, template_rows)

    result = scorer.score_reviews(
        r1_path, key1_path, blind1, r2_path, key2_path, blind2, adjudication,
    )
    assert result["paired_difference"]["n_cases"] == 2
    assert result["paired_difference"]["new_minus_old_successes"] == 0
    assert result["final_per_arm"]["old_workflow"]["strict_success_count"] == 1
    assert result["final_per_arm"]["new_fact_verification"]["strict_success_count"] == 1
    assert result["adjudication"]["disputed_candidate_count"] == 1
    assert result["interrater_before_adjudication"]["correctness"]["n"] == 4
    assert result["label_counts_before_adjudication"]["reviewer1"]["correctness"]["正确"] == 3
    assert result["label_counts_after_adjudication"]["correctness"]["正确"] == 3


def test_human_review_scoring_rejects_missing_adjudication(tmp_path):
    key1_path, key2_path = tmp_path / "key1.json", tmp_path / "key2.json"
    _write_json(key1_path, _key("A"))
    _write_json(key2_path, _key("B"))
    r1_path, r2_path = tmp_path / "r1.jsonl", tmp_path / "r2.jsonl"
    _write_jsonl(r1_path, [_rating("A1", "正确", "充分"), _rating("A2", "正确", "充分"),
                           _rating("A3", "正确", "充分"), _rating("A4", "正确", "充分")])
    _write_jsonl(r2_path, [_rating("B1", "错误", "不充分"), _rating("B2", "正确", "充分"),
                           _rating("B3", "正确", "充分"), _rating("B4", "正确", "充分")])
    blind1, blind2 = tmp_path / "blind1.json", tmp_path / "blind2.json"
    _write_json(blind1, {"packet_sha256": "a" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    _write_json(blind2, {"packet_sha256": "b" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    adjudication = tmp_path / "adjudication.jsonl"
    _write_jsonl(adjudication, [])
    with pytest.raises(ValueError, match="第三人裁决记录"):
        scorer.score_reviews(r1_path, key1_path, blind1, r2_path, key2_path, blind2, adjudication)


def test_scoring_rejects_reuse_of_same_blind_key(tmp_path):
    key_path = tmp_path / "same_key.json"
    _write_json(key_path, _key("A"))
    with pytest.raises(ValueError, match="相同匿名评审包"):
        scorer._require_independent_packets(scorer._read_key(key_path), "a" * 64,
                                            scorer._read_key(key_path), "a" * 64)


def test_scoring_rejects_same_arm_label_mapping_even_if_packet_hash_differs(tmp_path):
    key1_path, key2_path = tmp_path / "key1.json", tmp_path / "key2.json"
    first = _key("A")
    second = json.loads(json.dumps(first))
    second["review_packet_sha256"] = "b" * 64
    _write_json(key1_path, first)
    _write_json(key2_path, second)
    with pytest.raises(ValueError, match="匿名标签映射相同"):
        scorer._require_independent_packets(scorer._read_key(key1_path), "a" * 64,
                                            scorer._read_key(key2_path), "b" * 64)


def test_human_review_scoring_accepts_matched_reference_arm_names(tmp_path):
    def matched_key(prefix: str, reverse: bool) -> dict:
        labels = {"matched_reference_ablation": "B", "new_fact_verification": "A"}
        if reverse:
            labels = {arm: ("A" if label == "B" else "B") for arm, label in labels.items()}
        return {
            "candidate_mapping": [
                {"candidate_id": prefix + str(index), "case_id": case_id, "arm": arm,
                 "blind_label": label}
                for index, (case_id, arm, label) in enumerate(
                    [("C1", "matched_reference_ablation", labels["matched_reference_ablation"]),
                     ("C1", "new_fact_verification", labels["new_fact_verification"]),
                     ("C2", "matched_reference_ablation", labels["matched_reference_ablation"]),
                     ("C2", "new_fact_verification", labels["new_fact_verification"])],
                    start=1,
                )
            ],
            "review_packet_sha256": ("a" if prefix == "A" else "b") * 64,
            "arm_label_mapping": {label: arm for arm, label in labels.items()},
        }

    key1, key2 = tmp_path / "key1.json", tmp_path / "key2.json"
    _write_json(key1, matched_key("A", False))
    _write_json(key2, matched_key("B", True))
    ratings1, ratings2 = tmp_path / "r1.jsonl", tmp_path / "r2.jsonl"
    _write_jsonl(ratings1, [_rating("A1", "正确", "充分"), _rating("A2", "错误", "充分"),
                           _rating("A3", "正确", "充分"), _rating("A4", "错误", "充分")])
    _write_jsonl(ratings2, [_rating("B1", "正确", "充分"), _rating("B2", "错误", "充分"),
                           _rating("B3", "正确", "充分"), _rating("B4", "错误", "充分")])
    template = scorer.build_adjudication_template(ratings1, key1, ratings2, key2)
    adjudication = tmp_path / "adjudication.jsonl"
    _write_jsonl(adjudication, [json.loads(line) for line in template.splitlines()])
    blind1, blind2 = tmp_path / "blind1.json", tmp_path / "blind2.json"
    _write_json(blind1, {"packet_sha256": "a" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    _write_json(blind2, {"packet_sha256": "b" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    result = scorer.score_reviews(ratings1, key1, blind1, ratings2, key2, blind2, adjudication)
    assert result["paired_difference"]["reference_arm"] == "matched_reference_ablation"
    assert result["paired_difference"]["new_minus_reference_successes"] == -2


def test_primary_human_status_policy_is_arm_agnostic(tmp_path):
    """Primary success is the same blinded correctness/evidence rule for either arm."""
    key1_path, key2_path = tmp_path / "key1.json", tmp_path / "key2.json"
    _write_json(key1_path, _key("A"))
    _write_json(key2_path, _key("B"))
    r1_path, r2_path = tmp_path / "r1.jsonl", tmp_path / "r2.jsonl"
    ratings1 = [_rating(f"A{i}", "正确", "充分") for i in range(1, 5)]
    ratings2 = [_rating(f"B{i}", "正确", "充分") for i in range(1, 5)]
    _write_jsonl(r1_path, ratings1)
    _write_jsonl(r2_path, ratings2)
    blind1, blind2 = tmp_path / "blind1.json", tmp_path / "blind2.json"
    _write_json(blind1, {"packet_sha256": "a" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    _write_json(blind2, {"packet_sha256": "b" * 64, "label_mapping_guess": "cannot_judge", "confidence": "低"})
    adjudication = tmp_path / "adjudication.jsonl"
    _write_jsonl(adjudication, [])

    result = scorer.score_reviews(
        r1_path, key1_path, blind1, r2_path, key2_path, blind2, adjudication,
    )
    assert result["final_per_arm"]["old_workflow"]["strict_success_count"] == 2
    assert result["final_per_arm"]["new_fact_verification"]["strict_success_count"] == 2
    assert all(row["strict_evidence_supported_success"]
               for row in result["candidate_level_final_labels"])
