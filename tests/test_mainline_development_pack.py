import json
import runpy
from pathlib import Path

import pytest

# t4_v02/t4_v03 题库 jsonl 被 .gitignore 排除（eval/mainline/ 只入库了 t3 部分），
# 仅存在于运行机。CI 缺数据时整文件跳过。
from conftest import require_private_data

pytestmark = require_private_data("mainline_t4")

ROOT = Path(__file__).resolve().parents[1]


def test_development_pack_has_question_only_runtime_files_and_separate_rubrics() -> None:
    builder = runpy.run_path(str(ROOT / "eval" / "mainline" / "build_development_v01.py"))
    artifacts = builder["build_artifacts"]()

    expected = {"t3": 6, "t4": 8, "t5": 10}
    for task, count in expected.items():
        rows = [
            json.loads(line)
            for line in artifacts[f"{task}_questions.jsonl"].decode("utf-8").splitlines()
        ]
        assert len(rows) == count
        assert all(set(row) <= {"id", "task", "question", "turns"} for row in rows)
        assert all(row["task"] == int(task[-1]) for row in rows)

    manifest = json.loads(artifacts["manifest.json"].decode("utf-8"))
    assert manifest["dataset_kind"] == "exposed_development_pilot_not_holdout"
    assert manifest["gold_in_runtime_inputs"] is False
    for name, expected_hash in manifest["artifact_sha256"].items():
        import hashlib

        assert hashlib.sha256(artifacts[name]).hexdigest() == expected_hash


def test_development_rubrics_are_not_embedded_in_runtime_questions() -> None:
    builder = runpy.run_path(str(ROOT / "eval" / "mainline" / "build_development_v01.py"))
    artifacts = builder["build_artifacts"]()

    for task in ("t3", "t4", "t5"):
        question_bytes = artifacts[f"{task}_questions.jsonl"]
        rubric_bytes = artifacts[f"{task}_rubric.jsonl"]
        assert b"gold_sql" not in question_bytes
        assert b"required_tables" not in question_bytes
        assert b"gold_sql" not in rubric_bytes


def test_t4_v02_development_pack_has_20_isolated_cases() -> None:
    import hashlib

    dev_dir = ROOT / "eval" / "mainline" / "development_v0.1"
    questions = [
        json.loads(line)
        for line in (dev_dir / "t4_v02_questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rubrics = [
        json.loads(line)
        for line in (dev_dir / "t4_v02_rubric.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    references = [
        json.loads(line)
        for line in (dev_dir / "t4_v02_reference_queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    question_ids = {row["id"] for row in questions}

    assert len(questions) == len(rubrics) == len(references) == 20
    assert len(question_ids) == 20
    assert question_ids == {row["id"] for row in rubrics} == {row["id"] for row in references}
    assert all(set(row) == {"id", "task", "question"} and row["task"] == 4 for row in questions)
    assert all(row["exposure"] == "exposed_development_material" for row in rubrics)

    manifest = json.loads((dev_dir / "t4_v02_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_kind"] == "exposed_development_regression_not_holdout"
    assert manifest["case_count"] == 20
    assert manifest["gold_in_runtime_inputs"] is False
    runtime_manifest = json.loads((dev_dir / "t4_v02_runtime_manifest.json").read_text(encoding="utf-8"))
    historical_sidecars = {
        "schema_meta.adventureworks.aliases.zh.json": runtime_manifest["schema_resources"]["aliases"]["sha256"],
        "schema_meta.adventureworks.descriptions.zh.json": runtime_manifest["schema_resources"]["descriptions"]["sha256"],
    }
    for filename, expected_hash in manifest["artifact_sha256"].items():
        if filename in historical_sidecars:
            # The v0.2 run used earlier sidecars; current metadata evolved for v0.3.
            assert expected_hash == historical_sidecars[filename]
            continue
        candidate = ROOT / "infra" / filename if filename.startswith("schema_meta.") else dev_dir / filename
        assert hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash


def test_t4_v03_development_pack_matches_frozen_manifest() -> None:
    import hashlib

    dev_dir = ROOT / "eval" / "mainline" / "development_v0.1"
    questions = [
        json.loads(line)
        for line in (dev_dir / "t4_v03_questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rubrics = [
        json.loads(line)
        for line in (dev_dir / "t4_v03_rubric.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    references = [
        json.loads(line)
        for line in (dev_dir / "t4_v03_reference_queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    question_ids = {row["id"] for row in questions}

    assert len(questions) == len(rubrics) == len(references) == 20
    assert len(question_ids) == 20
    assert question_ids == {row["id"] for row in rubrics} == {row["id"] for row in references}
    assert all(set(row) == {"id", "task", "question"} and row["task"] == 4 for row in questions)
    assert all(row["exposure"] == "exposed_development_material" for row in rubrics)

    manifest = json.loads((dev_dir / "t4_v03_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_kind"] == "exposed_development_regression_not_holdout"
    assert manifest["version"] == "t4-development-v0.3"
    assert manifest["case_count"] == 20
    assert manifest["gold_in_runtime_inputs"] is False
    for filename, expected_hash in manifest["artifact_sha256"].items():
        candidate = ROOT / "infra" / filename if filename.startswith("schema_meta.") else dev_dir / filename
        assert hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash


def test_t5_v02_pack_is_question_only_and_matches_manifest() -> None:
    import hashlib

    dev_dir = ROOT / "eval" / "mainline" / "development_v0.1"
    questions = [
        json.loads(line)
        for line in (dev_dir / "t5_v02_questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rubric = [
        json.loads(line)
        for line in (dev_dir / "t5_v02_rubric.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    references = [
        json.loads(line)
        for line in (dev_dir / "t5_v02_reference_cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = {row["id"] for row in questions}

    assert len(questions) == len(rubric) == len(references) == 20
    assert len(ids) == 20
    assert ids == {row["id"] for row in rubric} == {row["id"] for row in references}
    assert all(set(row) == {"id", "task", "question"} and row["task"] == 5 for row in questions)
    assert all(row["exposure"] == "previously_exposed_development_material" for row in rubric)

    manifest = json.loads((dev_dir / "t5_v02_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_kind"] == "exposed_development_regression_not_holdout"
    assert manifest["case_count"] == 20
    assert manifest["gold_in_runtime_questions"] is False
    for filename, expected_hash in manifest["artifact_sha256"].items():
        candidate = ROOT / "infra" / filename if filename.startswith("schema_meta.") else dev_dir / filename
        assert hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash
