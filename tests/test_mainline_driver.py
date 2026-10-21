from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from eval.mainline import driver


@pytest.mark.parametrize(
    "relative_path",
    [
        "eval/mainline/development_v0.1/t3_rubric.jsonl",
        "eval/mainline/development_v0.1/t4_rubric.jsonl",
        "eval/mainline/development_v0.1/t5_rubric.jsonl",
        "eval/mainline/development_v0.1/t4_reference_queries.jsonl",
        "eval/mainline/development_v0.1/manifest.json",
    ],
)
def test_answer_bearing_evaluation_files_are_excluded_from_source_manifest(relative_path: str) -> None:
    assert driver._is_answer_bearing_eval_file(relative_path)
    assert driver._is_answer_bearing_eval_file(relative_path.replace("/", "\\"))
    assert not driver._is_answer_bearing_eval_file(
        "eval/mainline/development_v0.1/t4_questions.jsonl"
    )


def test_load_cases_rejects_answer_fields_and_duplicate_ids(tmp_path: Path) -> None:
    case_file = tmp_path / "questions.jsonl"
    case_file.write_text(
        json.dumps({"id": "T3-01", "task": 3, "turns": ["增长率是多少？", "销售额同比"]}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert driver.load_cases(case_file) == [
        {
            "id": "T3-01",
            "task": 3,
            "turns": [
                {"question": "增长率是多少？", "session": "default"},
                {"question": "销售额同比", "session": "default"},
            ],
        }
    ]

    case_file.write_text(
        json.dumps({"id": "T3-01", "task": 3, "question": "增长率是多少？", "gold": "secret"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="非运行字段"):
        driver.load_cases(case_file)

    case_file.write_text(
        '{"id":"T3-01","task":3,"question":"甲"}\n'
        '{"id":"T3-01","task":3,"question":"乙"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        driver.load_cases(case_file)


def test_freeze_replace_retries_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "freeze.json.tmp"
    destination = tmp_path / "freeze.json"
    temporary.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    original_replace = Path.replace
    attempts = 0

    def flaky_replace(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient Windows file lock")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    driver._replace_with_bounded_retry(temporary, destination)

    assert attempts == 3
    assert destination.read_text(encoding="utf-8") == "new"
    assert not temporary.exists()


def test_run_isolates_cases_and_reuses_session_within_multiturn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_file = tmp_path / "questions.jsonl"
    case_file.write_text(
        '{"id":"T3-01","task":3,"turns":["缺少指标的问题",{"session":"other","question":"另一个会话"},{"session":"default","question":"补充指标"}]}\n'
        '{"id":"T5-01","task":5,"question":"一个单轮查询"}\n',
        encoding="utf-8",
    )
    calls: list[tuple[str, dict | None]] = []

    def fake_request(url: str, payload: dict | None, timeout: float) -> tuple[int, dict]:
        calls.append((url, payload))
        if url.endswith("/health"):
            return 200, {"status": "ok", "graph_backend": "mock"}
        return 200, {
            "answer": "response",
            "trace": [],
            "refs": [],
            "sql": None,
            "need_clarify": False,
        }

    monkeypatch.setattr(driver, "request_json", fake_request)
    out_dir = tmp_path / "out"
    args = Namespace(
        cases=case_file,
        base_url="http://example.test",
        out_dir=out_dir,
        run_id="test-run",
        repeat=2,
        timeout=5,
        data_manifest=None,
        runtime_manifest=None,
    )

    driver.run(args)

    rows = [json.loads(line) for line in (out_dir / "raw_answers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 8
    t3_rows = [row for row in rows if row["case_id"] == "T3-01"]
    assert len(t3_rows) == 6
    for repeat_index in (1, 2):
        turns = sorted((row for row in t3_rows if row["repeat"] == repeat_index), key=lambda row: row["turn_index"])
        assert turns[0]["request"]["session_id"] == turns[2]["request"]["session_id"]
        assert turns[0]["request"]["session_id"] != turns[1]["request"]["session_id"]
    assert len({row["request"]["session_id"] for row in rows}) == 6
    assert all(row["response"]["answer"] == "response" for row in rows)
    metadata = json.loads((out_dir / "freeze.json").read_text(encoding="utf-8"))
    assert metadata["answer_key_read_by_driver"] is False
    assert metadata["answer_key_files_excluded_from_source_manifest"] is True
    assert metadata["capture_status"] == "completed"
    assert metadata["recorded_turns"] == 8
    assert calls[0][0].endswith("/health")


def test_call_ask_preserves_actual_sql_without_claiming_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "answer": "候选 SQL",
        "trace": [{"tool": "nl2sql", "output": {"executed": False}}],
        "refs": [],
        "sql": "SELECT 1",
        "need_clarify": False,
    }

    monkeypatch.setattr(driver, "request_json", lambda *_args: (200, body))
    result = driver.call_ask("http://example.test", "查询", "session-1", 5)

    assert result["response"] == body
    assert "executed" not in result


def test_call_ask_preserves_protocol_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"answer": "partial response"}
    monkeypatch.setattr(driver, "request_json", lambda *_args: (200, body))

    result = driver.call_ask("http://example.test", "查询", "session-1", 5)

    assert result["response"] == body
    assert result["error"]["type"] == "ProtocolError"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test",
        "https://example.test/?token=secret",
        "https://example.test/private-path",
    ],
)
def test_normalize_base_url_rejects_credential_bearing_or_non_origin_urls(url: str) -> None:
    with pytest.raises(ValueError):
        driver.normalize_base_url(url)


def test_run_marks_interrupted_capture_in_freeze_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_file = tmp_path / "questions.jsonl"
    case_file.write_text(
        '{"id":"T5-01","task":5,"question":"first"}\n'
        '{"id":"T5-02","task":5,"question":"second"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        driver,
        "request_json",
        lambda url, *_args: (200, {"status": "ok"}) if url.endswith("/health") else (200, {}),
    )
    call_count = 0

    def fake_call(*_args: object) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise KeyboardInterrupt
        return {"error": None, "turn_index": 1, "request": {}, "response": {}}

    monkeypatch.setattr(driver, "call_ask", fake_call)
    out_dir = tmp_path / "interrupted"
    args = Namespace(
        cases=case_file,
        base_url="http://example.test",
        out_dir=out_dir,
        run_id="interrupt-test",
        repeat=1,
        timeout=5,
        data_manifest=None,
        runtime_manifest=None,
    )

    with pytest.raises(KeyboardInterrupt):
        driver.run(args)

    metadata = json.loads((out_dir / "freeze.json").read_text(encoding="utf-8"))
    assert metadata["capture_status"] == "interrupted"
    assert metadata["completed_case_runs"] == 1
    assert (out_dir / "raw_answers.jsonl").is_file()
    assert len((out_dir / "raw_answers.jsonl").read_text(encoding="utf-8").splitlines()) == 1

