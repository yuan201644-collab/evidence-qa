from __future__ import annotations

import os

from app.model_profiles import (
    ModelProfileDefinition,
    get_active_profile,
    get_default_profile_id,
    model_profile_scope,
    public_profile_catalog,
)
from app.rag.llm import OpenAILikeClient
from app.contracts import AskResponse
from app import main as main_module
from app import model_config_store
from app import model_profiles
from fastapi.testclient import TestClient

from conftest import login_client


def test_model_catalog_exposes_only_safe_metadata(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret-deepseek")
    monkeypatch.setenv("LLM_API_BASE", "https://ark.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("GLM4_API_KEY", "secret-glm")

    catalog = public_profile_catalog()
    assert get_default_profile_id() == "deepseek-v4-1-flash"
    assert [item["id"] for item in catalog] == ["deepseek-v4-1-flash", "glm-4-flash"]
    assert all(item["configured"] for item in catalog)
    rendered = repr(catalog)
    assert "secret-deepseek" not in rendered
    assert "secret-glm" not in rendered
    assert "api_key" not in rendered


def test_unconfigured_profile_is_visible_but_cannot_be_selected(monkeypatch, auth_client):
    monkeypatch.setenv("LLM_API_KEY", "secret-deepseek")
    monkeypatch.setenv("LLM_API_BASE", "https://ark.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.delenv("GLM4_API_KEY", raising=False)

    catalog = public_profile_catalog()
    assert catalog[0]["configured"] is True
    assert catalog[1]["configured"] is False
    try:
        with model_profile_scope("glm-4-flash"):
            raise AssertionError("unconfigured profile should not enter the scope")
    except ValueError as exc:
        assert "尚未在后端配置凭据" in str(exc)
    assert get_active_profile() is None
    # auth_client：登录门之后 /api/ask 需要会话，未登录会先得到 401 而非这里的 400。
    response = auth_client.post(
        "/api/ask",
        json={"question": "controlled", "session_id": "s"},
        headers={"X-Model-Profile": "glm-4-flash"},
    )
    assert response.status_code == 400


def test_client_uses_request_scoped_profile_and_restores_default(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret-deepseek")
    monkeypatch.setenv("LLM_API_BASE", "https://ark.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("GLM4_API_KEY", "secret-glm")
    monkeypatch.setenv("GLM4_API_BASE", "https://glm.example/v4")
    monkeypatch.setenv("GLM4_MODEL", "glm-4-flash")
    monkeypatch.delenv("LLM_MOCK", raising=False)

    client = OpenAILikeClient(api_key="default-key", base_url="https://default/v1", model="default-model")
    calls = []
    monkeypatch.setattr(client, "_call", lambda system, user, **kwargs: calls.append(kwargs) or "ok")

    assert client.chat("system", "user") == "ok"
    assert calls[-1] == {}
    with model_profile_scope("glm-4-flash"):
        assert client.chat("system", "user") == "ok"
        assert calls[-1] == {
            "api_key": "secret-glm",
            "base_url": "https://glm.example/v4",
            "model": "glm-4-flash",
        }
        assert get_active_profile() is not None
    assert get_active_profile() is None
    assert client.chat("system", "user") == "ok"
    assert calls[-1] == {}


def test_model_catalog_endpoint_does_not_return_api_keys(monkeypatch, auth_client):
    monkeypatch.setenv("LLM_API_KEY", "catalog-secret-one")
    monkeypatch.setenv("LLM_API_BASE", "https://ark.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("GLM4_API_KEY", "catalog-secret-two")
    response = auth_client.get("/api/models")
    assert response.status_code == 200
    assert response.json()["default_model_id"] == "deepseek-v4-1-flash"
    assert [item["id"] for item in response.json()["models"]] == [
        "deepseek-v4-1-flash", "glm-4-flash"
    ]
    assert "catalog-secret-one" not in response.text
    assert "catalog-secret-two" not in response.text
    assert all("api_key" not in item for item in response.json()["models"])


def test_model_config_endpoint_persists_values_without_returning_key(monkeypatch, tmp_path, auth_store):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://localhost/test\n", encoding="utf-8")
    monkeypatch.setattr(model_config_store, "MODEL_ENV_FILE", env_file)
    for name in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE", "DEEPSEEK_MODEL",
        "LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    with TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.1", 8000)) as client:
        login_client(client, auth_store)
        initial = client.get("/api/model-config")
        assert initial.status_code == 200
        assert "profile-secret-from-test" not in initial.text
        response = client.put(
            "/api/model-config/deepseek-v4-1-flash",
            json={
                "model": "deepseek-v4.1-flash-test",
                "base_url": "https://ark.example.com/api/v1/",
                "api_key": "profile-secret-from-test",
            },
        )
        assert response.status_code == 200
        assert "profile-secret-from-test" not in response.text
        assert response.json()["profile"]["has_api_key"] is True
        assert response.json()["profile"]["configured"] is True
        update_without_key = client.put(
            "/api/model-config/deepseek-v4-1-flash",
            json={
                "model": "deepseek-v4.1-flash-updated",
                "base_url": "https://ark.example.com/api/v1",
                "api_key": "",
            },
        )
        assert update_without_key.status_code == 200
        assert update_without_key.json()["profile"]["has_api_key"] is True
        assert "profile-secret-from-test" not in update_without_key.text

    saved_env = env_file.read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql://localhost/test" in saved_env
    assert "DEEPSEEK_MODEL=deepseek-v4.1-flash-updated" in saved_env
    assert "DEEPSEEK_API_BASE=https://ark.example.com/api/v1" in saved_env
    assert "DEEPSEEK_API_KEY=profile-secret-from-test" in saved_env
    assert get_profile_value("DEEPSEEK_API_KEY") == "profile-secret-from-test"


def test_model_config_rejects_insecure_public_api_and_hides_key(monkeypatch, tmp_path, auth_store):
    monkeypatch.setattr(model_config_store, "MODEL_ENV_FILE", tmp_path / ".env")
    with TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.1", 8000)) as client:
        login_client(client, auth_store)
        response = client.put(
            "/api/model-config/glm-4-flash",
            json={
                "model": "glm-4-flash",
                "base_url": "http://api.example.com/v1",
                "api_key": "must-not-echo-this-secret",
            },
        )
    assert response.status_code == 422
    assert "HTTPS" in response.text
    assert "must-not-echo-this-secret" not in response.text


def test_model_config_requires_admin_role(monkeypatch, tmp_path, auth_store):
    """模型配置由「角色」而非「来源 IP」把关。

    历史：本用例原为 `test_model_config_rejects_non_loopback_clients`，断言来自
    `192.0.2.20` 的客户端一律 403，对应当时 app/main.py 的
    `_require_local_model_config_access()`（`ipaddress.ip_address(host).is_loopback`）。
    账号系统落地时该函数被**有意替换**为 `_require_admin()`，理由见
    docs/账号系统设计_v0.1.md:7「管理员能管理账号、模型配置、评测与项目证据」：
    按来源 IP 判断在反向代理后必然失真（所有请求都来自代理的 127.0.0.1），
    而按角色判断在代理前后都成立。因此断言随之改为「普通用户 403、管理员放行」。
    """
    env_file = tmp_path / ".env"
    monkeypatch.setattr(model_config_store, "MODEL_ENV_FILE", env_file)
    store, admin_name, admin_password = auth_store
    store.create_user("conftest-plain-user", "conftest-plain-password-1", role="user")

    with TestClient(main_module.app, base_url="https://testserver", client=("192.0.2.20", 8000)) as client:
        # 管理员：即便来源是非回环地址，也应放行（这正是替换 IP 校验的目的——
        # 部署在反向代理后时，管理员请求的 client.host 同样是代理地址）。
        login_client(client, auth_store)
        admin_read = client.get("/api/model-config")
        assert admin_read.status_code == 200

        # 普通用户：读和写都必须 403，且不泄漏密钥、不落盘。
        client.cookies.clear()
        plain_login = client.post(
            "/api/auth/login",
            json={"username": "conftest-plain-user", "password": "conftest-plain-password-1"},
        )
        assert plain_login.status_code == 200, plain_login.text
        user_read = client.get("/api/model-config")
        user_write = client.put(
            "/api/model-config/glm-4-flash",
            json={
                "model": "glm-4-flash",
                "base_url": "https://glm.example/v4",
                "api_key": "remote-client-secret",
            },
        )
    assert user_read.status_code == 403
    assert user_write.status_code == 403
    assert "remote-client-secret" not in user_write.text
    assert not env_file.exists()


def test_new_registry_entry_flows_to_catalog_and_settings_api(monkeypatch, tmp_path, auth_store):
    definition = ModelProfileDefinition(
        id="future-model-v9",
        label="Future Model V9",
        provider="示例服务商",
        model_env_key="FUTURE_V9_MODEL",
        base_url_env_key="FUTURE_V9_API_BASE",
        api_key_env_key="FUTURE_V9_API_KEY",
        default_model="future-model-v9",
        default_base_url="https://future.example/v1",
        legacy_model_markers=("future-model",),
    )
    monkeypatch.setattr(
        model_profiles,
        "MODEL_PROFILE_DEFINITIONS",
        (*model_profiles.MODEL_PROFILE_DEFINITIONS, definition),
    )
    monkeypatch.setattr(model_config_store, "MODEL_ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("LLM_MODEL", "future-model-v9")
    for key in ("FUTURE_V9_MODEL", "FUTURE_V9_API_BASE", "FUTURE_V9_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    with TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.1", 8000)) as client:
        login_client(client, auth_store)
        model_catalog = client.get("/api/models")
        editable_catalog = client.get("/api/model-config")
        assert model_catalog.status_code == 200
        assert model_catalog.json()["default_model_id"] == "future-model-v9"
        assert model_catalog.json()["models"][-1]["id"] == "future-model-v9"
        assert editable_catalog.status_code == 200
        assert editable_catalog.json()["profiles"][-1]["id"] == "future-model-v9"

        saved = client.put(
            "/api/model-config/future-model-v9",
            json={
                "model": "future-model-v9-chat",
                "base_url": "https://future.example/v2",
                "api_key": "future-registry-secret",
            },
        )

    assert saved.status_code == 200
    assert saved.json()["profile"]["configured"] is True
    assert "future-registry-secret" not in saved.text
    persisted = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FUTURE_V9_MODEL=future-model-v9-chat" in persisted
    assert "FUTURE_V9_API_BASE=https://future.example/v2" in persisted
    assert "FUTURE_V9_API_KEY=future-registry-secret" in persisted


def get_profile_value(name: str) -> str | None:
    """Read a test environment value without placing it in test output."""
    return os.environ.get(name)


def test_ask_header_selects_model_only_for_that_request(monkeypatch, auth_client):
    monkeypatch.setenv("LLM_API_KEY", "request-secret-one")
    monkeypatch.setenv("LLM_API_BASE", "https://ark.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("GLM4_API_KEY", "request-secret-two")
    seen = []

    class StubOrchestrator:
        def answer(self, question, session_id="default", *, progress_callback=None):
            profile = get_active_profile()
            seen.append(profile.id if profile else None)
            return AskResponse(answer="controlled", trace=[], refs=[], sql=None, need_clarify=False)

    monkeypatch.setattr(main_module, "_orchestrator", StubOrchestrator())
    client = auth_client
    selected = client.post(
        "/api/ask",
        json={"question": "controlled", "session_id": "s"},
        headers={"X-Model-Profile": "glm-4-flash"},
    )
    streamed = client.post(
        "/api/ask/stream",
        json={"question": "controlled", "session_id": "s"},
        headers={"X-Model-Profile": "glm-4-flash"},
    )
    default = client.post(
        "/api/ask",
        json={"question": "controlled", "session_id": "s"},
    )
    invalid = client.post(
        "/api/ask",
        json={"question": "controlled", "session_id": "s"},
        headers={"X-Model-Profile": "not-a-profile"},
    )
    assert selected.status_code == 200
    assert streamed.status_code == 200
    assert '"type":"complete"' in streamed.text
    assert default.status_code == 200
    assert invalid.status_code == 400
    assert seen == ["glm-4-flash", "glm-4-flash", None]
