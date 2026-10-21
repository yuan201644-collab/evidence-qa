from __future__ import annotations

from types import SimpleNamespace

from app import main as main_module
from app.tools.cross_source_demo import CrossSourceDemoTool


def test_demo_readiness_fails_closed_without_dsn(monkeypatch):
    monkeypatch.delenv("CROSS_SOURCE_DEMO_DSN", raising=False)
    tool = CrossSourceDemoTool(dsn="")

    assert tool.readiness() == (False, "demo_database_not_configured")


def test_demo_readiness_checks_docs_and_read_only_seed_query(monkeypatch, tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "contract.md").write_text("合同条款", encoding="utf-8")
    monkeypatch.setenv("CROSS_SOURCE_DEMO_DOCS_DIR", str(docs))
    tool = CrossSourceDemoTool(dsn="postgresql://user:secret@localhost/cross_source_demo")
    calls: list[str] = []

    def load_records(work_order_id, _budget):
        calls.append(work_order_id)
        return ([{"work_order_id": work_order_id}], [{"event_type": "accepted"}])

    monkeypatch.setattr(tool, "_load_records", load_records)

    assert tool.readiness() == (True, "ready")
    assert calls == ["WO-2026-0001"]


def test_demo_readiness_reports_empty_seed_without_connection_details(monkeypatch, tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "contract.md").write_text("合同条款", encoding="utf-8")
    monkeypatch.setenv("CROSS_SOURCE_DEMO_DOCS_DIR", str(docs))
    tool = CrossSourceDemoTool(dsn="postgresql://user:secret@localhost/cross_source_demo")
    monkeypatch.setattr(tool, "_load_records", lambda *_args: ([], []))

    assert tool.readiness() == (False, "demo_seed_work_order_missing")


def test_cross_source_capability_is_disabled_when_route_not_registered(auth_client, monkeypatch):
    class Registry:
        @staticmethod
        def get(_name):
            return None

    monkeypatch.setattr(main_module, "_orchestrator", SimpleNamespace(registry=Registry()))

    response = auth_client.get("/api/capabilities/cross-source-demo")

    assert response.status_code == 200
    assert response.json() == {
        "status": "disabled",
        "ready": False,
        "reason_code": "demo_route_disabled",
    }


def test_cross_source_capability_probes_registered_route(auth_client, monkeypatch):
    class Registry:
        @staticmethod
        def get(name):
            assert name == "cross_source_demo"
            return SimpleNamespace(readiness=lambda: (True, "ready"))

    monkeypatch.setattr(main_module, "_orchestrator", SimpleNamespace(registry=Registry()))

    response = auth_client.get("/api/capabilities/cross-source-demo")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "ready": True, "reason_code": "ready"}


def test_cross_source_capability_hides_probe_exceptions(auth_client, monkeypatch):
    class Registry:
        @staticmethod
        def get(_name):
            def fail():
                raise RuntimeError("postgresql://user:secret@localhost/cross_source_demo")

            return SimpleNamespace(readiness=fail)

    monkeypatch.setattr(main_module, "_orchestrator", SimpleNamespace(registry=Registry()))

    response = auth_client.get("/api/capabilities/cross-source-demo")

    assert response.status_code == 200
    assert response.json() == {
        "status": "unavailable",
        "ready": False,
        "reason_code": "demo_readiness_check_failed",
    }
    assert "secret" not in response.text
