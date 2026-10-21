import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

import run_architecture_v01 as runner  # noqa: E402


def test_runtime_package_and_question_fields_are_fail_closed():
    assert len(runner.runtime_manifest()) == 24
    questions = runner.load_questions()
    assert len(questions) == 12
    assert all(set(question) == runner.QUESTION_FIELDS for question in questions)


def test_source_manifest_excludes_unrelated_eval_experiments():
    manifest = runner.source_manifest()
    assert "eval/cross_source_heldout/tools/run_architecture_v01.py" in manifest
    assert "eval/experiments/evont_next/evaluate_task4_holdout_sql_v01.py" not in manifest
