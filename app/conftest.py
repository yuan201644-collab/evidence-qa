"""app/ 下用例的共享工具。

与 `tests/conftest.py` 同源：登录门（`app/main.py::require_authenticated_api`）
上线后，`/api/` 端点未带会话一律 401，而 app/ 下不少用例是直接 `TestClient(app)`
请求这些端点的。它们的断言没错，只是缺登录态。

这里的 fixture 直接从 `tests/conftest.py` 复用同一套实现，避免两处各写一遍后
行为漂移（比如密码长度校验、临时库位置）。`tests/` 与 `app/` 是谁先 import
`app.main` 由 pytest 收集顺序决定，但两处都只是 monkeypatch 模块级
`_auth_store`，不依赖 import 时机，所以复用是安全的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[1] / "tests"
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from conftest import (  # noqa: E402  (需先补 sys.path)
    auth_client,
    auth_headers,
    auth_store,
    auth_token,
    login_client,
    require_private_data,
)

__all__ = [
    "auth_client",
    "auth_headers",
    "auth_store",
    "auth_token",
    "login_client",
    "require_private_data",
]
