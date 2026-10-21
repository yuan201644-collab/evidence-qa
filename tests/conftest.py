"""tests/ 共享的 pytest 工具。

当前只有一个用途：为依赖「运行机私有数据」的测试提供统一的模块级/用例级
跳过开关。这些数据位于被 .gitignore 排除的目录（eval/cross_source_heldout/
下的候选运行包、eval/mainline/development_v0.1 的 t4 题库等），只存在于
跑过实验的机器上；CI checkout 后不存在，测试若直接读会 FileNotFoundError。
"""

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent

# 依次列出各测试文件依赖的私有数据锚点（锚点存在 => 数据大概率完整）。
_PRIVATE_ANCHORS: dict[str, Path] = {
    "cs14_candidate": PROJECT_ROOT / "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.3_candidate/runtime/questions.jsonl",
    "cs17_candidate": PROJECT_ROOT / "eval/cross_source_heldout/2026Q4_fresh_cs17_v0.6_candidate/runtime/SHA256SUMS.txt",
    "cs18_candidate": PROJECT_ROOT / "eval/cross_source_heldout/2026Q4_fresh_cs18_v0.6_candidate/runtime/questions.jsonl",
    "mainline_t4": PROJECT_ROOT / "eval/mainline/development_v0.1/t4_v02_rubric.jsonl",
}


def require_private_data(*keys: str):
    """返回 pytest.mark.skipif 装饰器：任一锚点缺失则跳过该测试。

    用法：
        @require_private_data("cs14_candidate")
        def test_cs14_route_preflight_uses_candidate_runtime_aliases(): ...
    """
    missing = [k for k in keys if not _PRIVATE_ANCHORS[k].exists()]
    if missing:
        reason = "运行机私有数据不在本 checkout 中（CI 环境）: " + ", ".join(missing)
        return pytest.mark.skip(reason=reason)
    return pytest.mark.skipif(False, reason="")


# ---------------------------------------------------------------------------
# 登录门（app/main.py::require_authenticated_api）之后的测试辅助
# ---------------------------------------------------------------------------
#
# 背景：`/api/` 与前端构建资源默认要求已登录会话，未登录一律 401。大量既有
# 用例走 `TestClient(app)` 直接请求这些端点，加门之后集体变红——它们的断言本身
# 没错，只是**没有登录态**。这里提供统一的注入方式，避免每个用例各写一遍
# 建库/建号/建会话的样板代码。
#
# 为什么用 monkeypatch 替换 `_auth_store` 而不是往真实账号库里塞测试账号：
#   真实库路径由 VERIMIND_AUTH_DB_PATH 决定，跑测试时会落到开发者自己的账号库
#   上；往里写测试用户会污染本机数据，且并发跑测试时互相干扰。这里每个用例建
#   一个临时 SQLite，用完即弃。

_TEST_USERNAME = "conftest-user"
_TEST_PASSWORD = "conftest-password-1234"  # 长度须 >= 12（AuthStore 的校验）


@pytest.fixture
def auth_store(tmp_path, monkeypatch):
    """建一个临时账号库并让 app 使用它，返回 (store, 用户名, 密码)。

    `app.main` 在 import 期就实例化了 `_auth_store`，所以必须替换该模块级对象，
    而不是设置环境变量后重新 import。
    """
    from app import main as main_module
    from app.auth_store import AuthStore

    store = AuthStore(path=tmp_path / "conftest-accounts.sqlite3")
    store.create_user(_TEST_USERNAME, _TEST_PASSWORD, role="admin")
    monkeypatch.setattr(main_module, "_auth_store", store)
    return store, _TEST_USERNAME, _TEST_PASSWORD


@pytest.fixture
def auth_token(auth_store):
    """在临时账号库里建一个会话，返回可用于 Cookie 的 token。"""
    store, username, _password = auth_store
    user = store.get_user_by_username(username)
    assert user is not None
    return store.create_session(user.id)


@pytest.fixture
def auth_headers(auth_token):
    """给 TestClient 的请求带上登录 Cookie。

    用法：
        def test_x(auth_headers):
            with TestClient(main_module.app) as client:
                resp = client.get("/api/models", headers=auth_headers)
    """
    from app.main import _AUTH_COOKIE_NAME

    return {"Cookie": f"{_AUTH_COOKIE_NAME}={auth_token}"}


@pytest.fixture
def auth_client(auth_store, auth_headers):
    """已登录的 TestClient，可直接请求受保护的 `/api/` 端点。

    用法：
        def test_x(auth_client):
            resp = auth_client.get("/api/models")
            assert resp.status_code == 200

    注意：写请求（POST/PUT）仍会经过 `_verify_same_origin`。TestClient 发出的
    请求不带 `Origin` 头，会走 `Sec-Fetch-Site` 兜底分支被放行——这正是本次把
    「缺 Origin」从拒绝改为放行的目的之一，见 app/main.py::_verify_same_origin。
    """
    from fastapi.testclient import TestClient

    from app import main as main_module

    with TestClient(main_module.app) as client:
        client.headers.update(auth_headers)
        yield client


def login_client(client, auth_store) -> None:
    """就地给一个自建 TestClient 装上登录态。

    有些用例必须自建 `TestClient(app, client=(host, port))` 来模拟不同来源 IP，
    用不上 `auth_client` fixture。它们用这个函数补登录即可。

    实现上直接调 `POST /api/auth/login` 走真实登录链路（而不是手工塞 Cookie），
    顺带覆盖了登录接口本身在中间件白名单里的行为。
    """
    _store, username, password = auth_store
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, f"测试登录失败: {response.status_code} {response.text}"
