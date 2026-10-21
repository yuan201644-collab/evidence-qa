from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.request import Request

from eval.multiturn_runner import (
    _LoopbackSecureCookiePolicy,
    ask,
    authenticated_client,
    execution_summary,
    judge_turn,
    load_cases,
)


def _body(answer: str, normalized: str, *, sql: str | None = None,
          row_count: int | None = None, columns: list[str] | None = None) -> dict:
    output = {"row_count": row_count, "columns": columns or []}
    trace = [
        {"tool": "normalizer", "output": {"normalized": normalized}},
    ]
    if row_count is not None:
        trace.append({"tool": "nl2sql", "output": output})
    return {"answer": answer, "sql": sql, "trace": trace}


def test_wrong_answer_does_not_match_text_only_from_followup_context() -> None:
    turn = {
        "round": 2,
        "question": "他总共消费了多少？",
        "expect_contains": ["37.62"],
        "depends_on_context": True,
    }
    body = _body(
        "查询结果：total_consumption：2328.6。",
        "客户 Frank Harris 是谁？\n用户补充或修改：他总共消费了多少？",
    )

    result = judge_turn(turn, body, status=200, elapsed=1.0)

    assert not result.passed
    assert result.expect_missed == ["37.62"]


def test_missing_positive_assertion_fails_closed() -> None:
    turn = {
        "round": 5,
        "question": "这个最多的是哪个",
        "expect_contains": [],
        "depends_on_context": True,
    }
    body = _body(
        "随便编一个答案。",
        "所有发票总金额是多少？\n用户补充或修改：这个最多的是哪个",
    )

    result = judge_turn(turn, body, status=200, elapsed=1.0)

    assert not result.passed
    assert not result.has_answer_assertion
    assert "缺少答案正向断言" in result.note


def test_row_count_and_result_columns_are_checked() -> None:
    turn = {
        "round": 3,
        "question": "改成看发票张数",
        "expect_contains": [],
        "expect_row_count": 7,
        "expect_columns": ["invoice_count"],
        "depends_on_context": True,
    }
    body = _body(
        "查询结果：invoice_count：412。",
        "客户 Frank Harris 是谁？\n用户补充或修改：改成看发票张数",
        sql="SELECT COUNT(*) AS invoice_count FROM invoice",
        row_count=1,
        columns=["invoice_count"],
    )

    summary = execution_summary(body)
    result = judge_turn(turn, body, status=200, elapsed=1.0)

    assert summary == (1, ["invoice_count"])
    assert not result.passed
    assert result.expected_row_count == 7
    assert result.actual_row_count == 1


def test_sql_context_assertions_are_checked_independently() -> None:
    turn = {
        "round": 3,
        "question": "改成按月拆开",
        "expect_contains": ["2022-01"],
        "expect_sql_contains": ["invoice_month"],
        "expect_sql_absent": ["billing_country"],
        "depends_on_context": True,
    }
    body = _body(
        "查询结果：billing_country：USA。",
        "所有发票总金额是多少？\n用户补充或修改：改成按月拆开",
        sql="SELECT billing_country, SUM(total) FROM invoice GROUP BY billing_country",
    )

    result = judge_turn(turn, body, status=200, elapsed=1.0)

    assert not result.passed
    assert result.expect_sql_missed == ["invoice_month"]
    assert result.expect_sql_absent_hits == ["billing_country"]
    assert result.actual_sql == "SELECT billing_country, SUM(total) FROM invoice GROUP BY billing_country"


def test_independent_turn_must_not_be_attached_to_previous_context() -> None:
    turn = {
        "round": 1,
        "question": "所有客户数量是多少？",
        "expect_contains": ["59"],
        "depends_on_context": False,
    }
    body = _body(
        "查询结果：customer_count：59。",
        "上一轮问题\n用户补充或修改：所有客户数量是多少？",
    )

    result = judge_turn(turn, body, status=200, elapsed=1.0)

    assert not result.passed
    assert "实际接上了上文" in result.note


def test_all_registered_turns_have_positive_answer_assertions() -> None:
    cases = load_cases()

    assert cases
    assert all(
        turn.get("expect_contains")
        or turn.get("expect_row_count") is not None
        or turn.get("expect_columns")
        for case in cases
        for turn in case.get("turns") or []
    )


def test_local_evaluation_returns_secure_login_cookie_over_loopback_http() -> None:
    class Handler(BaseHTTPRequestHandler):
        received_cookie = ""

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header(
                "Set-Cookie",
                "verimind_session=local-test-token; Path=/; HttpOnly; Secure; SameSite=Lax",
            )
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self) -> None:
            type(self).received_cookie = self.headers.get("Cookie", "")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        opener, origin = authenticated_client(base, "evaluator", "test-password", 2)
        with opener.open(Request(base + "/probe", headers={"Origin": origin}), timeout=2) as response:
            assert response.status == 200
        assert "verimind_session=local-test-token" in Handler.received_cookie
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_secure_cookie_over_http_is_allowed_only_for_loopback() -> None:
    policy = _LoopbackSecureCookiePolicy()
    secure_cookie = SimpleNamespace(secure=True)
    loopback_request = SimpleNamespace(type="http", host="127.0.0.1:8000")
    remote_request = SimpleNamespace(type="http", host="example.test")

    assert policy.return_ok_secure(secure_cookie, loopback_request)
    assert not policy.return_ok_secure(secure_cookie, remote_request)


def test_timeout_is_reported_as_round_failure_not_raised() -> None:
    """单轮超时必须记为该轮失败并继续。

    原先 ask() 只捕获 HTTPError / URLError，超时会把异常抛到顶层，整个评测脚本崩掉、
    前面已经跑完的轮次一份结果都不留 —— 这是长跑评测里最亏的失败方式。
    """
    import urllib.request

    class _TimingOutOpener(urllib.request.OpenerDirector):
        def open(self, *_args, **_kwargs):  # type: ignore[override]
            raise TimeoutError("timed out")

    status, body, elapsed_ms = ask(
        "http://127.0.0.1:1",
        "这个最多的是哪个",
        "session-timeout",
        1.0,
        opener=_TimingOutOpener(),
    )

    assert status == 0
    assert "超时" in body["error"]
    assert elapsed_ms >= 0
