"""mock 链路自检：证明编排层在「假工具」上已经真跑通。

跑法（项目根目录下）：
    D:/python/python.exe app/test_mock_pipeline.py

测什么（每条都对应一个容易写错的点）：
    1. 三个工具都能注册、都能出名片
    2. 三个工具各自的 mock 都返回完整 ToolResult（**必须含 trace 和 refs**）
    3. 路由分得清「算」「查」「串联」三种问题
    4. 跨源多跳链路的 trace 能合并、refs 能去重、答案拼得出来
    5. 澄清分支不调工具
    6. 图谱跳数上限生效
    7. GRAPH_BACKEND 开关能切后端
    8. HTTP /api/ask 真能跑通

为什么值得写这些：Mock A 的意义全在于「测到编排逻辑」。如果只测
「工具返回了 data」，那和没测一样 —— 编排层要处理的正是 trace 合并与
refs 去重这两件事。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contracts import DocRef, SqlRef, ToolResult
from app.orchestrator import build_default_orchestrator, route
from app.tools.base import CAPABILITY_DOC, CAPABILITY_GRAPH, CAPABILITY_TABLE
from app.tools.registry import ToolRegistry

PASSED = 0


def mock_orchestrator(graph_backend: str | None = None):
    """Mock 链路测试必须显式选择 mock，不能误加载真实模型。"""
    return build_default_orchestrator(
        graph_backend,
        doc_search_backend="mock",
        nl2sql_backend="mock",
    )


def check(name: str, fn) -> None:
    global PASSED
    try:
        fn()
    except AssertionError as e:
        print(f"[FAIL] {name}\n       {e}")
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {name}\n        {type(e).__name__}: {e}")
        raise
    PASSED += 1
    print(f"[ ok ] {name}")


# ── 1. 注册表 ────────────────────────────────────────────────────────────
def t_registry() -> None:
    orch = mock_orchestrator()
    reg = orch.registry
    assert reg.names() == ["doc_search", "graph_reason", "nl2sql"], reg.names()
    # 三个能力各有人覆盖
    assert len(reg.find_by_capability(CAPABILITY_TABLE)) >= 1
    assert len(reg.find_by_capability(CAPABILITY_DOC)) >= 1
    assert len(reg.find_by_capability(CAPABILITY_GRAPH)) >= 1
    # 重名要报错，不能静默覆盖
    dup = ToolRegistry()
    dup.register(reg.require("nl2sql"))
    try:
        dup.register(reg.require("nl2sql"))
    except ValueError as e:
        assert "重复" in str(e)
    else:
        raise AssertionError("重名工具应报错")
    # 名片导出（将来模型规划要用）
    described = reg.describe_all()
    assert len(described) == 3
    assert all(d["description"] for d in described), "每个工具都要有 description"


# ── 2. 每个工具都必须返回完整 ToolResult ────────────────────────────────
def t_each_tool_returns_full_result() -> None:
    orch = mock_orchestrator()
    for name in orch.registry.names():
        tool = orch.registry.require(name)
        result = tool.run({"question": "2024 年销售冠军是谁", "hint": "李伟"})
        assert isinstance(result, ToolResult), name + " 未返回 ToolResult"
        assert len(result.trace) >= 1, name + " 没返回 trace（编排层就测不到了）"
        assert len(result.refs) >= 1, name + " 没返回 refs（答案会变成无引用）"
        assert all(s.tool == name for s in result.trace), name + " 的 trace.tool 写错了"
        assert all(r.snippet for r in result.refs), name + " 的 refs 里有空 snippet"


# ── 3. 路由分得清三类问题 ───────────────────────────────────────────────
def t_route_three_kinds() -> None:
    orch = mock_orchestrator()
    reg = orch.registry

    # 「算」：只要结构化
    p = route("2024 年销售额排名第一的是谁", reg)
    assert [n for n, _ in p.steps] == ["nl2sql"], p.steps

    # 「查」：只要文档
    p = route("员工报销流程是怎么规定的", reg)
    assert [n for n, _ in p.steps] == ["doc_search"], p.steps

    # 文档题含泛化量词时仍优先走 RAG，不能被"多少"误路由到 SQL
    p = route("维保合同的质保期是多少", reg)
    assert [n for n, _ in p.steps] == ["doc_search"], p.steps

    # 「串联」：走图谱
    p = route("2024 年销售冠军是谁，并且他的成功方法是什么", reg)
    assert [n for n, _ in p.steps] == ["graph_reason"], p.steps

    # 每种计划都必须说得清理由
    assert p.reason, "路由必须给出理由（可解释性要求）"
    assert p.confidence in ("high", "medium")


# ── 4. 跨源多跳链路：trace 合并 + refs 去重 + 答案拼装 ──────────────────
def t_cross_source_pipeline() -> None:
    orch = mock_orchestrator()
    resp = orch.answer("2024 年销售冠军是谁，并且客户 Leonie 的成功方法是什么", "s-cross")

    assert resp.need_clarify is False
    # normalizer + semantic_clarifier + 图谱 mock 2 跳 = 4 条 TraceStep，
    # 合并后 step_id 从 1 连续编号（10-02 起 normalizer 恒为第 1 步，
    # 见 docs/要素识别与改写_接口_v0.1.md §8）
    assert len(resp.trace) == 4, f"应有 4 步，实际 {len(resp.trace)}"
    assert [step.tool for step in resp.trace[:2]] == ["normalizer", "semantic_clarifier"]
    assert [s.step_id for s in resp.trace] == [1, 2, 3, 4], "合并后 step_id 必须重排成连续"
    assert {s.tool for s in resp.trace} == {
        "normalizer", "semantic_clarifier", "graph_reason"
    }, "tool 字段必须保留归属"
    assert resp.trace[0].tool == "normalizer" and resp.trace[0].hit is None

    # refs：两条引用（一条 sql 一条 doc）都要在，且无重复
    kinds = sorted(r.kind for r in resp.refs)
    assert kinds == ["doc", "sql"], f"两条引用都应在，实际 {kinds}"
    assert all(r.snippet for r in resp.refs), "snippet 不得为空（评测硬要求）"

    # 不变量：refs == 所有 trace.hit 的并集去重
    from app.contracts import collect_refs

    assert len(resp.refs) == len(collect_refs(resp.trace)), "refs 与 trace.hit 不一致"

    # 答案要真的拼出来，不能是空串
    assert resp.answer and "先记账" in resp.answer, resp.answer

    # 序列化往返（HTTP 会做这件事）
    back = type(resp).model_validate_json(resp.model_dump_json())
    assert len(back.refs) == len(resp.refs)
    assert len(back.trace) == len(resp.trace)


# ── 5. refs 去重：重复命中不能重复计数 ──────────────────────────────────
def t_refs_dedup() -> None:
    from app.contracts import TraceStep, collect_refs

    same = SqlRef(table="chinook.invoice", column="total", row_pk="customer_id=42", snippet="重复命中")
    step_a = TraceStep(step_id=1, tool="graph_reason", elapsed_ms=1.0, hit=[same])
    step_b = TraceStep(step_id=2, tool="graph_reason", elapsed_ms=1.0, hit=[same])
    refs = collect_refs([step_a, step_b])
    assert len(refs) == 1, "同一条引用命中两次，去重后应只剩 1 条"


# ── 6. 澄清分支：不调任何工具 ───────────────────────────────────────────
def t_clarify() -> None:
    orch = mock_orchestrator()
    resp = orch.answer("查一下", "s-clarify")
    assert resp.need_clarify is True
    # 10-02 起：改写已经发生就必须留痕（接口文档 §8.2 第 3 条）——
    # 澄清分支不调任何业务工具，但预处理与澄清判定都必须留痕
    assert [s.tool for s in resp.trace] == ["normalizer", "semantic_clarifier"], (
        "澄清分支应记录改写与澄清判定，不应调用业务工具"
    )
    assert all(s.hit is None for s in resp.trace)
    assert resp.refs == []
    assert resp.answer, "澄清时 answer 装反问句（契约约定复用 answer 字段）"


# ── 7. 图谱跳数上限生效 ─────────────────────────────────────────────────
def t_hop_limit() -> None:
    orch = mock_orchestrator()
    tool = orch.registry.require("graph_reason")

    normal = tool.run({"question": "销售冠军的成功方法", "hint": "李伟"})
    assert len(normal.trace) == 2, "默认应走 2 跳"

    limited = tool.run({"question": "销售冠军的成功方法", "hint": "李伟", "max_hops": 1})
    assert len(limited.trace) == 1, "限制成 1 跳时应只回 1 步"

    # 超过硬上限要被压回来，不能真让它跳 5 跳
    capped = tool.run({"question": "销售冠军的成功方法", "hint": "李伟", "max_hops": 99})
    assert len(capped.trace) <= tool.MAX_HOPS, "跳数不得越过硬上限"


# ── 8. 后端开关 ─────────────────────────────────────────────────────────
def t_graph_backend_switch() -> None:
    import os

    mock_orch = mock_orchestrator("mock")
    assert mock_orch.graph_backend == "mock"
    assert mock_orch.registry.has("graph_reason"), "无论哪个后端，工具名必须一致"

    kag_orch = mock_orchestrator("kag")
    assert kag_orch.graph_backend == "kag"
    # 工具名一致 → 编排层感知不到后端差别（统一工具协议的意义）
    assert kag_orch.registry.names() == mock_orch.registry.names()

    # 环境变量也能生效
    os.environ["GRAPH_BACKEND"] = "mock"
    try:
        from_env = mock_orchestrator()
        assert from_env.graph_backend == "mock"
    finally:
        os.environ.pop("GRAPH_BACKEND", None)


# ── 9. HTTP 接口真跑通 ──────────────────────────────────────────────────
def t_http_ask(auth_headers=None) -> None:
    """HTTP 冒烟：登录门之后必须带会话才能拿到 200。

    `auth_headers` 由 `app/conftest.py` 注入（pytest 路径）。直接
    `python app/test_mock_pipeline.py` 跑本文件时没有 fixture，就自建一套
    临时账号库——与 conftest 的做法一致，但只在这个脚本入口生效。
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("       （跳过：未装 httpx，TestClient 不可用）")
        return

    from app.main import app

    if auth_headers is None:
        import tempfile
        from pathlib import Path

        from app import main as main_module
        from app.auth_store import AuthStore
        from app.main import _AUTH_COOKIE_NAME

        tmp = Path(tempfile.mkdtemp(prefix="mock-pipeline-auth-"))
        store = AuthStore(path=tmp / "accounts.sqlite3")
        user = store.create_user("mock-pipeline", "mock-pipeline-pass-1234", role="admin")
        main_module._auth_store = store
        auth_headers = {"Cookie": f"{_AUTH_COOKIE_NAME}={store.create_session(user.id)}"}

    client = TestClient(app, headers=auth_headers)

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.post(
        "/api/ask",
        json={"question": "2024 年销售冠军是谁，并且客户 Leonie 的成功方法是什么", "session_id": "s-http"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    expected_keys = {"answer", "trace", "refs", "sql", "need_clarify"}
    assert (
        expected_keys
        <= set(body.keys())
        <= expected_keys | {"result_set", "suggestions", "data_source", "context_continuation"}
    ), body.keys()
    assert body["need_clarify"] is False
    assert len(body["refs"]) == 2
    assert len(body["trace"]) == 4  # normalizer + semantic_clarifier + 图谱 2 跳
    assert body["answer"]

    # 契约是 extra="forbid"：多传字段应该被拒，而不是静默忽略
    r = client.post("/api/ask", json={"question": "x", "session_id": "s", "extra": 1})
    assert r.status_code == 422, "多余字段必须报错（契约 extra=forbid）"


if __name__ == "__main__":
    tests = [
        ("注册表登记 / 查名 / 按能力筛 / 重名报错", t_registry),
        ("三个工具都返回完整 ToolResult（含 trace + refs）", t_each_tool_returns_full_result),
        ("路由分得清「算」「查」「串联」", t_route_three_kinds),
        ("跨源多跳：trace 合并 + refs 齐全 + 答案拼装", t_cross_source_pipeline),
        ("refs 去重（重复命中只算一条）", t_refs_dedup),
        ("澄清分支不调工具", t_clarify),
        ("图谱跳数上限生效", t_hop_limit),
        ("GRAPH_BACKEND 开关可切后端", t_graph_backend_switch),
        ("HTTP /api/ask 真跑通", t_http_ask),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
