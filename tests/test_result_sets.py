import csv
import io

from fastapi.testclient import TestClient

from app import main as main_module
from app.contracts import AskResponse, ToolResult
from app.orchestrator import Orchestrator, ToolRun
from app.result_sets import ResultSetStore
from app.tools.registry import ToolRegistry


def _rows(count: int):
    return [{"id": index, "name": f"item-{index}"} for index in range(count)]


def test_store_pages_in_order_and_expires_entries():
    now = [10.0]
    store = ResultSetStore(
        ttl_seconds=2,
        max_sets=2,
        max_rows=10,
        max_bytes=4096,
        page_size=2,
        clock=lambda: now[0],
    )
    info = store.put(["id", "name"], _rows(5), complete=True)

    assert info is not None
    assert info.row_count == 5
    assert info.complete is True
    first = store.page(info.id, offset=0, page_size=2)
    second = store.page(info.id, offset=2, page_size=2)
    last = store.page(info.id, offset=4, page_size=2)
    assert first is not None and [row["id"] for row in first.rows] == [0, 1]
    assert first.next_offset == 2
    assert second is not None and [row["id"] for row in second.rows] == [2, 3]
    assert second.next_offset == 4
    assert last is not None and [row["id"] for row in last.rows] == [4]
    assert last.next_offset is None

    now[0] = 13.0
    assert store.page(info.id) is None


def test_store_does_not_export_partial_results_or_accept_oversized_pages():
    store = ResultSetStore(page_size=2, max_rows=4, max_bytes=4096)
    info = store.put(["id"], _rows(5), complete=False)

    assert info is not None
    assert info.complete is False
    assert info.row_count == 4
    assert store.csv_bytes(info.id) is None
    try:
        store.page(info.id, offset=0, page_size=201)
    except ValueError as exc:
        assert "page_size" in str(exc)
    else:
        raise AssertionError("oversized page_size should be rejected")


def test_store_evicts_oldest_entry_when_set_limit_is_reached():
    store = ResultSetStore(max_sets=1, max_rows=10, max_bytes=4096, page_size=1)
    first = store.put(["id"], _rows(2), complete=True)
    second = store.put(["id"], _rows(2), complete=True)

    assert first is not None and second is not None
    assert store.page(first.id) is None
    assert store.page(second.id) is not None


def test_store_enforces_production_row_limit_of_5000():
    store = ResultSetStore(max_rows=5000, max_bytes=16 * 1024 * 1024, page_size=100)
    info = store.put(["id"], _rows(5001), complete=True)

    assert info is not None
    assert info.row_count == 5000
    assert info.complete is False
    assert store.page(info.id, offset=4900, page_size=100) is not None
    assert store.csv_bytes(info.id) is None


def test_store_enforces_production_cache_byte_limit():
    max_bytes = 16 * 1024 * 1024
    store = ResultSetStore(max_rows=5000, max_bytes=max_bytes, page_size=100)
    rows = [{"payload": "x" * 17_000} for _ in range(1000)]

    info = store.put(["payload"], rows, complete=True)

    assert info is not None
    assert 100 < info.row_count < len(rows)
    assert info.complete is False
    assert store._total_bytes <= max_bytes
    assert store.csv_bytes(info.id) is None


def test_store_evicts_oldest_entry_when_aggregate_byte_limit_is_reached():
    store = ResultSetStore(max_sets=4, max_rows=10, max_bytes=2048, page_size=1)
    rows = [{"id": value, "payload": "x" * 600} for value in range(3)]
    first = store.put(["id", "payload"], rows, complete=True)
    second = store.put(["id", "payload"], rows, complete=True)

    assert first is not None and second is not None
    assert store.page(first.id) is None
    assert store.page(second.id) is not None
    assert store._total_bytes <= store.max_bytes


def test_csv_preserves_column_order_and_all_rows():
    store = ResultSetStore(max_rows=10, max_bytes=4096, page_size=1)
    info = store.put(
        ["name", "id"],
        [{"name": "=1+1", "id": 1}, {"name": "item-2", "id": 2}, {"name": "item-3", "id": 3}],
        complete=True,
    )

    assert info is not None
    payload = store.csv_bytes(info.id)
    assert payload is not None
    decoded = payload.decode("utf-8-sig")
    assert list(csv.reader(io.StringIO(decoded))) == [
        ["name", "id"],
        ["'=1+1", "1"],
        ["item-2", "2"],
        ["item-3", "3"],
    ]


def test_api_serves_pages_and_rejects_partial_csv(auth_store, auth_headers):
    client = TestClient(main_module.app, headers=auth_headers)
    original_store = main_module._orchestrator.result_set_store
    store = ResultSetStore(max_rows=10, max_bytes=4096, page_size=2)
    main_module._orchestrator.result_set_store = store
    # 接口侧会用 app/main.py::_scoped_session_id 把客户端的 X-Session-ID 按登录
    # 用户加前缀后再查结果集，所以直接写 store 时必须用同一个键——
    # 这里复用产品函数而不是手写拼接，避免前缀规则变化后测试悄悄失配。
    user = auth_store[0].get_user_by_username(auth_store[1])
    scoped_a = main_module._scoped_session_id(user, "session-a")
    scoped_b = main_module._scoped_session_id(user, "session-b")
    try:
        complete = store.put(["id", "name"], _rows(5), complete=True, session_id=scoped_a)
        assert complete is not None
        page_response = client.get(
            f"/api/results/{complete.id}?offset=2&page_size=2",
            headers={"X-Session-ID": "session-a"},
        )
        assert page_response.status_code == 200
        assert [row["id"] for row in page_response.json()["rows"]] == [2, 3]
        missing_session = client.get(f"/api/results/{complete.id}?offset=0&page_size=2")
        assert missing_session.status_code == 422
        wrong_session = client.get(
            f"/api/results/{complete.id}?offset=0&page_size=2",
            headers={"X-Session-ID": "session-b"},
        )
        assert wrong_session.status_code == 404
        wrong_session_export = client.get(
            f"/api/results/{complete.id}/export.csv",
            headers={"X-Session-ID": "session-b"},
        )
        assert wrong_session_export.status_code == 404
        csv_response = client.get(
            f"/api/results/{complete.id}/export.csv",
            headers={"X-Session-ID": "session-a"},
        )
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")

        partial = store.put(["id", "name"], _rows(5), complete=False, session_id=scoped_a)
        assert partial is not None
        blocked = client.get(
            f"/api/results/{partial.id}/export.csv",
            headers={"X-Session-ID": "session-a"},
        )
        assert blocked.status_code == 409
        missing = client.get(
            "/api/results/not-a-real-result-id",
            headers={"X-Session-ID": "session-a"},
        )
        assert missing.status_code == 404
    finally:
        main_module._orchestrator.result_set_store = original_store


def test_orchestrator_exposes_only_result_set_metadata_in_ask_contract():
    store = ResultSetStore(max_rows=10, max_bytes=4096, page_size=2)
    orchestrator = Orchestrator(ToolRegistry(), result_set_store=store)
    run = ToolRun(
        tool_name="nl2sql",
        reason="structured query",
        result=ToolResult(
            data={
                "rows": _rows(5),
                "columns": ["id", "name"],
                "possible_truncation": False,
            },
        ),
        elapsed_ms=0,
    )

    info = orchestrator._create_result_set([run], session_id="session-a")
    response = AskResponse(answer="完整结果已附在下方。", result_set=info)

    assert response.result_set is not None
    assert response.result_set.complete is True
    assert "rows" not in response.model_dump(mode="json")["result_set"]
