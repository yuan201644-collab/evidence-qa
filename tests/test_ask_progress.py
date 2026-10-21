from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.contracts import AskResponse, FactReview, FactReviewItem, SqlRef, TraceStep
from app.progress import emit_progress, progress_scope
from app import main as main_module


def test_progress_events_pair_start_and_completion_and_ignore_callback_failure():
    events = []
    with progress_scope(events.append):
        emit_progress("rag_retrieve", "检索相关文档片段", "started")
        emit_progress("rag_retrieve", "检索相关文档片段", "completed", elapsed_ms=12.34)
    assert events[0]["id"] == events[1]["id"]
    assert [event["status"] for event in events] == ["started", "completed"]
    assert events[1]["elapsed_ms"] == 12.3

    with progress_scope(lambda _event: (_ for _ in ()).throw(RuntimeError("closed client"))):
        emit_progress("sql_execute", "执行数据库只读查询", "started")
        emit_progress("sql_execute", "执行数据库只读查询", "completed", elapsed_ms=1)


def test_stream_endpoint_sends_stages_and_same_ask_response_contract(monkeypatch, auth_headers):
    response_model = AskResponse(
        answer="受控回答",
        trace=[],
        refs=[],
        sql=None,
        need_clarify=False,
        suggestions=[],
    )

    class StubOrchestrator:
        def answer(self, question, session_id="default", *, progress_callback=None):
            assert question == "测试问题"
            # 会话 ID 已被 app/main.py::_scoped_session_id 按登录用户加前缀
            # （`<user.id>:<sha256(客户端 session_id)>`），避免不同用户用同一个
            # 客户端 session_id 时互相串档。这里断言后缀契约，而不是原始字符串。
            assert session_id.endswith(
                ":84097828fc31a8c8d29210df48901a85de7fd013f686b17be77d1be29cb7a98b"
            ), session_id
            if progress_callback is not None:
                progress_callback({
                    "type": "stage",
                    "id": "stage-1",
                    "stage_id": "understand",
                    "label": "识别问题要素",
                    "status": "started",
                })
                progress_callback({
                    "type": "stage",
                    "id": "stage-1",
                    "stage_id": "understand",
                    "label": "识别问题要素",
                    "status": "completed",
                    "elapsed_ms": 2,
                })
            return response_model

    monkeypatch.setattr(main_module, "_orchestrator", StubOrchestrator())
    with TestClient(main_module.app, headers=auth_headers) as client:
        stream = client.post(
            "/api/ask/stream",
            json={"question": "测试问题", "session_id": "session-1"},
        )
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = [
            json.loads(line[6:])
            for line in stream.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [event["type"] for event in events] == ["stage", "stage", "complete", "end"]
        assert events[0]["id"] == events[1]["id"]
        expected_legacy = response_model.model_dump(mode="json")
        expected_legacy.pop("fact_review", None)
        assert events[2]["response"] == expected_legacy

        ordinary = client.post(
            "/api/ask",
            json={"question": "测试问题", "session_id": "session-1"},
        )
        assert ordinary.status_code == 200
        assert ordinary.json() == expected_legacy


def test_fact_review_header_is_opt_in_on_both_ask_paths(monkeypatch, auth_headers):
    review = FactReview(
        coverage="cross_source_work_order_sla",
        verification_mode="strict",
        verification_scope="fact_dependencies_and_business_checks",
        scope_note="仅展示事实账本。",
        items=[FactReviewItem(
            fact_id="work_order_facts",
            label="工单基本信息与事件",
            status="supported",
            execution_status="completed",
            evidence_link_status="complete",
            evidence_ref_indices=[0],
        )],
    )
    response_model = AskResponse(
        answer="受控回答",
        trace=[TraceStep(
            step_id=1, tool="nl2sql", input={}, output={}, elapsed_ms=1,
            hit=[SqlRef(
                table="invoices", column="total", result_id="query-result-1",
                snippet="查询结果总额 1234.5",
            )],
        )],
        refs=[SqlRef(
            table="invoices", column="total", result_id="query-result-1",
            snippet="查询结果总额 1234.5",
        )],
        sql=None, need_clarify=False,
        fact_review=review,
    )

    class StubOrchestrator:
        def answer(self, question, session_id="default", *, progress_callback=None):
            return response_model

    monkeypatch.setattr(main_module, "_orchestrator", StubOrchestrator())
    with TestClient(main_module.app, headers=auth_headers) as client:
        body = {"question": "测试问题", "session_id": "session-1"}
        client.headers.update(auth_headers)
        ordinary = client.post("/api/ask", json=body)
        assert ordinary.status_code == 200
        assert "fact_review" not in ordinary.json()
        assert "result_id" not in ordinary.json()["refs"][0]
        assert "result_id" not in ordinary.json()["trace"][0]["hit"][0]

        opted_in = client.post(
            "/api/ask", json=body, headers={"X-AskData-Fact-Review": "2"}
        )
        assert opted_in.status_code == 200
        assert opted_in.json()["fact_review"]["verification_mode"] == "strict"
        assert opted_in.json()["refs"][0]["result_id"] == "query-result-1"
        assert opted_in.json()["trace"][0]["hit"][0]["result_id"] == "query-result-1"
        streamed = client.post(
            "/api/ask/stream", json=body, headers={"X-AskData-Fact-Review": "2"}
        )
        assert streamed.status_code == 200
        events = [
            json.loads(line[6:])
            for line in streamed.text.splitlines()
            if line.startswith("data: ")
        ]
        complete = next(event for event in events if event["type"] == "complete")
        assert complete["response"]["fact_review"] == review.model_dump(mode="json")

        unsupported = client.post(
            "/api/ask", json=body, headers={"X-AskData-Fact-Review": "1"}
        )
        assert unsupported.status_code == 400
        unsupported_stream = client.post(
            "/api/ask/stream", json=body, headers={"X-AskData-Fact-Review": "1"}
        )
        assert unsupported_stream.status_code == 400
