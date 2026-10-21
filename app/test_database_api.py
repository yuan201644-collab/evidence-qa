from __future__ import annotations

from app import database_api
from app import main as main_module
from app.rag import config as _rag_config  # noqa: F401 - stabilize .env before test overrides
from app.nl2sql import executor as executor_module
from app.nl2sql.schema import SchemaCatalog
from app.contracts import UnstructuredDocumentInfo
from fastapi.testclient import TestClient


def _catalog() -> SchemaCatalog:
    return SchemaCatalog.from_dict({
        "database": "chinook",
        "dialect": "postgres",
        "contract_version": "test-1",
        "tables": [
            {
                "table": "album",
                "schema": "public",
                "description": "专辑目录",
                "row_count": 7,
                "primary_key": ["album_id"],
                "columns": [
                    {
                        "name": "album_id",
                        "type": "INT",
                        "nullable": False,
                        "is_primary_key": True,
                        "description": "专辑主键",
                        "sample_values": ["SECRET_SAMPLE"],
                    },
                    {
                        "name": "artist_id",
                        "type": "INT",
                        "nullable": False,
                        "description": "所属艺人",
                    },
                ],
                "foreign_keys": [
                    {
                        "columns": ["artist_id"],
                        "references": {"table": "artist", "columns": ["artist_id"]},
                    }
                ],
            },
            {
                "table": "artist",
                "schema": "public",
                "columns": [{"name": "artist_id", "type": "INT"}],
            },
        ],
    })


def test_database_schema_maps_safe_fields_and_omits_row_data(monkeypatch):
    monkeypatch.setattr(SchemaCatalog, "load", lambda: _catalog())

    result = database_api.build_database_schema().model_dump()
    album = next(table for table in result["tables"] if table["name"] == "album")

    assert result["database"] == "chinook"
    assert result["table_count"] == 2
    assert album["column_count"] == 2
    assert album["columns"][0]["is_primary_key"] is True
    assert album["columns"][1]["foreign_key_to"] == "artist.artist_id"
    assert "row_count" not in repr(result)
    assert "sample_values" not in repr(result)
    assert "SECRET_SAMPLE" not in repr(result)


def test_database_status_reports_connection_and_catalog_mismatch(monkeypatch):
    monkeypatch.setattr(SchemaCatalog, "load", lambda: _catalog())
    monkeypatch.setenv("NL2SQL_BACKEND", "real")

    class ConnectedExecutor:
        def __init__(self, **_kwargs):
            pass

        def database_name(self) -> str:
            return "chinook"

    monkeypatch.setattr(database_api, "SqlExecutor", ConnectedExecutor)
    assert database_api.build_database_status().status == "connected"

    ConnectedExecutor.database_name = lambda self: "other_db"
    mismatched = database_api.build_database_status()
    assert mismatched.status == "catalog_mismatch"
    assert mismatched.database == "other_db"
    assert mismatched.catalog_database == "chinook"


def test_database_status_hides_driver_error_and_skips_mock_backend(monkeypatch):
    monkeypatch.setattr(SchemaCatalog, "load", lambda: _catalog())

    class BrokenExecutor:
        def __init__(self, **_kwargs):
            pass

        def database_name(self) -> str:
            raise RuntimeError("postgresql://ict:TOP_SECRET@db.internal/chinook")

    monkeypatch.setattr(database_api, "SqlExecutor", BrokenExecutor)
    monkeypatch.setenv("NL2SQL_BACKEND", "real")
    unavailable = database_api.build_database_status()
    assert unavailable.status == "unavailable"
    assert "TOP_SECRET" not in repr(unavailable)

    class UnexpectedExecutor:
        def __init__(self, **_kwargs):
            raise AssertionError("mock backend must not probe a real database")

    monkeypatch.setattr(database_api, "SqlExecutor", UnexpectedExecutor)
    monkeypatch.setenv("NL2SQL_BACKEND", "mock")
    assert database_api.build_database_status().status == "not_used"


def test_database_name_probe_sets_read_only_before_select(monkeypatch):
    calls: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql: str):
            assert connection.read_only is True
            calls.append(sql)

        def fetchone(self):
            return ("chinook",)

    class Connection:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    connection = Connection()

    class Psycopg:
        @staticmethod
        def connect(_dsn, *, connect_timeout, autocommit):
            assert connect_timeout == 3
            assert autocommit is False
            return connection

    monkeypatch.setattr(executor_module, "_require_psycopg", lambda: (Psycopg, None))
    result = executor_module.SqlExecutor(connect_timeout_seconds=3).database_name()

    assert result == "chinook"
    assert calls == ["SELECT current_database()"]


def test_database_schema_route_serializes_schema_alias_and_safe_fields(monkeypatch, auth_headers):
    monkeypatch.setattr(main_module, "build_database_schema", database_api.build_database_schema)
    monkeypatch.setattr(SchemaCatalog, "load", lambda: _catalog())

    with TestClient(main_module.app) as client:
        response = client.get("/api/database/schema", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["tables"][0]["schema"] == "public"
    assert "schema_name" not in payload["tables"][0]
    assert "SECRET_SAMPLE" not in response.text


def test_unstructured_status_reports_memory_backend_and_pg_dsn_mismatch(monkeypatch):
    monkeypatch.setattr(database_api, "_corpus_documents", lambda: [
        UnstructuredDocumentInfo(doc_id="KB-001", title="KB-001", extension="pdf"),
    ])
    monkeypatch.delenv("RAG_DSN", raising=False)
    monkeypatch.setenv("PG_DSN", "postgresql://user:secret@localhost/db")
    monkeypatch.setenv("DOC_SEARCH_BACKEND", "rag")

    result = database_api.build_unstructured_database()

    assert result.status == "memory"
    assert result.backend == "memory"
    assert result.corpus_document_count == 1
    assert "RAG_DSN" in result.warning
    assert "secret" not in repr(result)


def test_unstructured_status_reads_loaded_memory_index_without_building_it(monkeypatch):
    monkeypatch.setattr(database_api, "_corpus_documents", lambda: [
        UnstructuredDocumentInfo(doc_id="KB-001", title="KB-001", extension="docx"),
        UnstructuredDocumentInfo(doc_id="KB-002", title="KB-002", extension="md"),
    ])
    monkeypatch.delenv("RAG_DSN", raising=False)
    monkeypatch.setenv("DOC_SEARCH_BACKEND", "rag")

    class Registry:
        def get(self, name):
            assert name == "doc_search"
            chunks = [
                type("Chunk", (), {"doc_id": "KB-001"})(),
                type("Chunk", (), {"doc_id": "KB-001"})(),
            ]
            index = type("Index", (), {"chunks": chunks})()
            rag_tool = type("Rag", (), {"corpus_index": index})()
            return type("Tool", (), {"_rag_tool": rag_tool})()

    orchestrator = type("Orchestrator", (), {"registry": Registry()})()
    result = database_api.build_unstructured_database(orchestrator)

    assert result.status == "memory"
    assert result.indexed_document_count == 1
    assert result.chunk_count == 2
    assert result.documents[0].indexed_chunk_count == 2
    assert result.documents[1].indexed_chunk_count == 0


def test_pgvector_inspection_is_read_only_and_returns_schema_and_counts(monkeypatch):
    monkeypatch.setattr(database_api, "_corpus_documents", lambda: [
        UnstructuredDocumentInfo(doc_id="KB-001", title="KB-001", extension="pdf"),
    ])
    monkeypatch.setenv("DOC_SEARCH_BACKEND", "rag")
    monkeypatch.setenv("RAG_DSN", "postgresql://user:secret@localhost/vector")
    executed = []

    class Cursor:
        current_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=None):
            assert connection.read_only is True
            assert sql.lstrip().upper().startswith("SELECT")
            executed.append(sql)
            self.current_sql = sql

        def fetchone(self):
            return ("rag", "public", True)

        def fetchall(self):
            if "information_schema.columns" in self.current_sql:
                return [
                    ("chunk_vectors", "id", "integer", "int4", "NO"),
                    ("chunk_vectors", "doc_id", "text", "text", "NO"),
                    ("doc_meta", "doc_id", "text", "text", "NO"),
                ]
            if "table_constraints" in self.current_sql:
                return [("chunk_vectors", "id"), ("doc_meta", "doc_id")]
            if "GROUP BY doc_id" in self.current_sql:
                return [("KB-001", 4)]
            return []

    class Connection:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    connection = Connection()

    class Psycopg:
        @staticmethod
        def connect(dsn, *, connect_timeout, options):
            assert "secret" in dsn
            assert connect_timeout == 3
            assert "statement_timeout=3000" in options
            return connection

    monkeypatch.setitem(__import__("sys").modules, "psycopg", Psycopg)

    result = database_api.build_unstructured_database()

    assert result.status == "connected"
    assert result.database == "rag"
    assert result.indexed_document_count == 1
    assert result.chunk_count == 4
    assert result.documents[0].indexed_chunk_count == 4
    assert [table.name for table in result.tables] == ["chunk_vectors", "doc_meta"]
    assert result.tables[0].columns[0].is_primary_key is True
    assert len(executed) == 4
    assert "secret" not in repr(result)


def test_pgvector_probe_failure_hides_dsn(monkeypatch):
    monkeypatch.setattr(database_api, "_corpus_documents", lambda: [])
    monkeypatch.setenv("DOC_SEARCH_BACKEND", "rag")
    monkeypatch.setenv("RAG_DSN", "postgresql://user:TOP_SECRET@host/vector")

    class BrokenPsycopg:
        @staticmethod
        def connect(*_args, **_kwargs):
            raise RuntimeError("connection failed: TOP_SECRET")

    monkeypatch.setitem(__import__("sys").modules, "psycopg", BrokenPsycopg)
    result = database_api.build_unstructured_database()

    assert result.status == "unavailable"
    assert "TOP_SECRET" not in repr(result)


def test_unstructured_route_serializes_schema_alias_and_hides_database_errors(monkeypatch, auth_headers):
    response_model = database_api.UnstructuredDatabaseResponse(
        status="connected",
        backend="pgvector",
        label="PostgreSQL 文档向量库",
        database="rag",
        schema_name="public",
        corpus_document_count=1,
        indexed_document_count=1,
        chunk_count=4,
        checked_at="2026-10-13T00:00:00Z",
        tables=[database_api.DatabaseTableInfo(
            schema_name="public",
            name="chunk_vectors",
            column_count=1,
            columns=[database_api.DatabaseColumnInfo(name="doc_id", type="text")],
        )],
    )
    monkeypatch.setattr(
        main_module,
        "build_unstructured_database",
        lambda _orchestrator: response_model,
    )

    with TestClient(main_module.app) as client:
        response = client.get("/api/database/unstructured", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["schema"] == "public"
    assert response.json()["tables"][0]["schema"] == "public"
    assert "schema_name" not in response.text


def test_unstructured_connection_status_reports_memory_without_database_details(monkeypatch):
    monkeypatch.setenv("DOC_SEARCH_BACKEND", "rag")
    monkeypatch.delenv("RAG_DSN", raising=False)

    result = database_api.build_unstructured_database_status()

    assert result.status == "memory"
    assert result.backend == "memory"
    assert "内存" in result.label
    assert "PG_DSN" not in repr(result)


def test_unstructured_connection_status_probes_read_only_and_hides_dsn(monkeypatch):
    monkeypatch.setenv("DOC_SEARCH_BACKEND", "rag")
    monkeypatch.setenv("RAG_DSN", "postgresql://user:TOP_SECRET@localhost/vector")
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            assert connection.read_only is True
            statements.append(sql)

        def fetchone(self):
            return (1,)

    class Connection:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    connection = Connection()

    class Psycopg:
        @staticmethod
        def connect(dsn, *, connect_timeout, options):
            assert "TOP_SECRET" in dsn
            assert connect_timeout == 3
            assert "statement_timeout=3000" in options
            return connection

    monkeypatch.setitem(__import__("sys").modules, "psycopg", Psycopg)

    result = database_api.build_unstructured_database_status()

    assert result.status == "connected"
    assert result.backend == "pgvector"
    assert statements == ["SELECT 1"]
    assert "TOP_SECRET" not in repr(result)


def test_unstructured_connection_status_route_returns_only_connection_fields(monkeypatch, auth_headers):
    result = database_api.UnstructuredDatabaseStatusResponse(
        status="memory",
        backend="memory",
        label="文档检索使用内存模式",
        checked_at="2026-10-13T00:00:00Z",
    )
    monkeypatch.setattr(
        main_module,
        "build_unstructured_database_status",
        lambda _orchestrator: result,
    )

    with TestClient(main_module.app) as client:
        response = client.get("/api/database/unstructured/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "memory"
    assert set(response.json()) == {"status", "backend", "label", "checked_at"}
