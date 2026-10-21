"""检查单镜像交付的静态模块类型；不调用模型或数据库。"""
from __future__ import annotations

import mimetypes
from pathlib import Path
import runpy
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import app.orchestrator


def test_windows_mjs_mime_does_not_break_pdf_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟 Windows 的 text/plain 映射，执行真实入口验证模块可交付。

    登录门（app/main.py::require_authenticated_api）之后，`/assets/*` 只对**已登录**
    会话开放：登录页是内联 HTML + CSS，只额外引用白名单里的品牌图标，所以匿名
    访客不需要静态资源；而登录之后的 PDF 工作线程要能取到 `.mjs`。本用例因此
    先登录再断言 `.mjs` 的内容类型，并额外确认匿名时确实被挡住（门是有效的）。

    `VERIMIND_AUTH_DB_PATH` 必须指向本用例自己的临时库：`load_env_file()` 从进程
    CWD（这里是真实工程根）读到 `.env` 里那条相对路径后，会相对**隔离实例**的
    `_project_root()`（即本用例的 tmp_path）解析，从而落到开发者本机的真实账号库上
    ——那既会污染真实账号，也会让重复运行撞 UNIQUE 约束。
    """
    monkeypatch.setenv("VERIMIND_AUTH_DB_PATH", str(tmp_path / "accounts.sqlite3"))
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    isolated_main = tmp_path / "app" / "main.py"
    isolated_main.parent.mkdir()
    isolated_main.write_bytes(source.read_bytes())
    assets = tmp_path / "web" / "dist" / "assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text("<html>static delivery</html>", encoding="utf-8")
    (assets / "pdf.worker.mjs").write_text("export const worker = true;", encoding="utf-8")
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".mjs", "text/plain")
    monkeypatch.setattr(
        app.orchestrator, "build_default_orchestrator",
        lambda: SimpleNamespace(graph_backend="mock"),
    )

    namespace = runpy.run_path(str(isolated_main))
    application = namespace["app"]
    # 隔离实例用自己的临时账号库（见上），建一个测试管理员用于登录，
    # 不触碰开发者本机的真实账号库。
    namespace["_auth_store"].create_user("static-delivery-user", "static-delivery-password", role="admin")

    # 正式默认 Cookie 带 Secure：测试也走 HTTPS，不能降低产品安全设置。
    with TestClient(application, base_url="https://testserver") as client:
        # 匿名：静态资源被登录门挡住（这是有意的，不是缺陷）。
        assert client.get("/assets/pdf.worker.mjs").status_code == 401
        # 登录页自身可匿名访问，且它只依赖白名单内的品牌图标与内联样式。
        assert client.get("/").status_code == 200
        assert client.get("/health").json()["status"] == "ok"

        login = client.post(
            "/api/auth/login",
            json={"username": "static-delivery-user", "password": "static-delivery-password"},
        )
        assert login.status_code == 200, login.text

        response = client.get("/assets/pdf.worker.mjs")
        assert response.status_code == 200
        assert response.headers["content-type"].split(";")[0] == "text/javascript"
        assert response.text == "export const worker = true;"
        assert client.get("/").status_code == 200
