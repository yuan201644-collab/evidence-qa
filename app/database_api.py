"""Safe read-only database status and schema responses for the workspace UI."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.contracts import (
    DatabaseColumnInfo,
    DatabaseSchemaResponse,
    DatabaseStatusResponse,
    DatabaseTableInfo,
    UnstructuredDatabaseResponse,
    UnstructuredDatabaseStatusResponse,
    UnstructuredDocumentInfo,
)
from app.nl2sql.config import env_str
from app.nl2sql.executor import SqlExecutor
from app.nl2sql.schema import SchemaCatalog, TableMeta


def build_database_status() -> DatabaseStatusResponse:
    """Probe the active SQL backend without returning connection details."""
    catalog = SchemaCatalog.load()
    source = catalog.data_source()
    catalog_database = source["database"] if source else None
    label = source["label"] if source else "未配置结构化数据源"
    dialect = source["dialect"] if source else (catalog.dialect or "postgres")
    table_count = len(catalog.tables)
    checked_at = datetime.now(timezone.utc).isoformat()

    if env_str("NL2SQL_BACKEND", "real").strip().lower() == "mock":
        return DatabaseStatusResponse(
            status="not_used",
            catalog_database=catalog_database,
            label=label,
            dialect=dialect,
            table_count=table_count,
            checked_at=checked_at,
        )

    try:
        connected_database = SqlExecutor(
            readonly=True,
            connect_timeout_seconds=3,
        ).database_name()
    except Exception:  # noqa: BLE001 - surface safe status, never a DSN-bearing driver error
        return DatabaseStatusResponse(
            status="unavailable",
            catalog_database=catalog_database,
            label=label,
            dialect=dialect,
            table_count=table_count,
            checked_at=checked_at,
        )

    mismatch = bool(
        catalog_database
        and connected_database.casefold() != catalog_database.casefold()
    )
    return DatabaseStatusResponse(
        status="catalog_mismatch" if mismatch else "connected",
        database=connected_database,
        catalog_database=catalog_database,
        label=label,
        dialect=dialect,
        table_count=table_count,
        checked_at=checked_at,
    )


def build_database_schema() -> DatabaseSchemaResponse:
    """Build the browser view from the same metadata catalog used by NL2SQL."""
    catalog = SchemaCatalog.load()
    source = catalog.data_source()
    tables = [
        _table_info(table)
        for table in sorted(
            catalog.tables.values(), key=lambda item: (item.schema.casefold(), item.name.casefold())
        )
    ]
    return DatabaseSchemaResponse(
        database=source["database"] if source else None,
        label=source["label"] if source else "未配置结构化数据源",
        dialect=source["dialect"] if source else (catalog.dialect or "postgres"),
        contract_version=catalog.contract_version,
        table_count=len(tables),
        tables=tables,
    )


def build_unstructured_database(orchestrator: Any = None) -> UnstructuredDatabaseResponse:
    """Inspect the actual document-search store using read-only database queries.

    This endpoint never constructs the lazy RAG tool and never initializes a schema:
    opening the panel must not load embedding models or write to either database.
    """
    # Importing the RAG package applies the same .env loading used by the actual
    # document-search path before we inspect its selected backend and DSN.
    from app.rag import config as _rag_config  # noqa: F401

    backend = _active_doc_search_backend(orchestrator)
    checked_at = datetime.now(timezone.utc).isoformat()
    documents = _corpus_documents()
    pg_dsn = (os.environ.get("RAG_DSN") or "").strip()

    if backend == "mock":
        return UnstructuredDatabaseResponse(
            status="not_used",
            backend="mock",
            label="文档检索当前使用模拟后端",
            corpus_document_count=len(documents),
            warning="模拟后端不会读取文档语料或向量库。",
            checked_at=checked_at,
            documents=documents,
        )

    if backend not in {"rag", "rag_multihop"}:
        return UnstructuredDatabaseResponse(
            status="unavailable",
            backend="mock",
            label="文档检索后端配置无法识别",
            corpus_document_count=len(documents),
            warning="请检查 DOC_SEARCH_BACKEND 配置。",
            checked_at=checked_at,
            documents=documents,
        )

    if not pg_dsn:
        corpus_index = _loaded_corpus_index(orchestrator)
        chunks = getattr(corpus_index, "chunks", None) if corpus_index else None
        chunk_counts = _chunk_counts(chunks) if isinstance(chunks, list) else None
        indexed_count = len(chunk_counts) if chunk_counts is not None else None
        chunk_count = sum(chunk_counts.values()) if chunk_counts is not None else None
        documents = _with_index_counts(documents, chunk_counts)
        warning = "当前使用进程内存向量库；服务重启后索引会丢失。"
        if (os.environ.get("PG_DSN") or "").strip():
            warning += " 检测到 PG_DSN，但问答检索读取的是 RAG_DSN；PG_DSN 不会自动启用持久化向量库。"
        if chunk_counts is None:
            warning += " 首次文档检索前，语料索引尚未载入内存。"
        return UnstructuredDatabaseResponse(
            status="memory",
            backend="memory",
            label="文档检索使用内存向量库",
            corpus_document_count=len(documents),
            indexed_document_count=indexed_count,
            chunk_count=chunk_count,
            warning=warning,
            checked_at=checked_at,
            documents=documents,
        )

    try:
        database, schema_name, vector_ready, tables, chunk_counts = _inspect_pgvector(pg_dsn)
    except Exception:  # noqa: BLE001 - driver errors can contain DSNs and credentials
        return UnstructuredDatabaseResponse(
            status="unavailable",
            backend="pgvector",
            label="文档向量库连接失败",
            corpus_document_count=len(documents),
            warning="无法连接 RAG_DSN 指向的 PostgreSQL；连接详情已隐藏。",
            checked_at=checked_at,
            documents=documents,
        )

    table_names = {table.name for table in tables}
    initialized = vector_ready and {"chunk_vectors", "doc_meta"}.issubset(table_names)
    documents = _with_index_counts(documents, chunk_counts if "chunk_vectors" in table_names else None)
    warning = None if initialized else "数据库可连接，但 pgvector 扩展或索引表尚未完整初始化。面板只读检查，不会自动建表。"
    actual_doc_count = len(chunk_counts) if "chunk_vectors" in table_names else None
    actual_chunk_count = sum(chunk_counts.values()) if "chunk_vectors" in table_names else None
    return UnstructuredDatabaseResponse(
        status="connected" if initialized else "not_initialized",
        backend="pgvector",
        label="PostgreSQL 文档向量库",
        database=database,
        schema_name=schema_name,
        corpus_document_count=len(documents),
        indexed_document_count=actual_doc_count,
        chunk_count=actual_chunk_count,
        vector_extension_ready=vector_ready,
        warning=warning,
        checked_at=checked_at,
        tables=tables,
        documents=documents,
    )


def build_unstructured_database_status(
    orchestrator: Any = None,
) -> UnstructuredDatabaseStatusResponse:
    """Check only whether the active document store has a database connection."""
    from app.rag import config as _rag_config  # noqa: F401 - load the RAG .env contract

    backend = _active_doc_search_backend(orchestrator)
    checked_at = datetime.now(timezone.utc).isoformat()
    if backend == "mock":
        return UnstructuredDatabaseStatusResponse(
            status="not_used",
            backend="mock",
            label="文档检索未启用真实数据库",
            checked_at=checked_at,
        )

    dsn = (os.environ.get("RAG_DSN") or "").strip()
    if not dsn:
        return UnstructuredDatabaseStatusResponse(
            status="memory",
            backend="memory",
            label="文档检索使用内存模式",
            checked_at=checked_at,
        )

    try:
        import psycopg

        with psycopg.connect(
            dsn,
            connect_timeout=3,
            options="-c statement_timeout=3000",
        ) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise RuntimeError("database connection probe failed")
    except Exception:  # noqa: BLE001 - driver errors can contain credentials
        return UnstructuredDatabaseStatusResponse(
            status="unavailable",
            backend="pgvector",
            label="文档向量库未连接",
            checked_at=checked_at,
        )

    return UnstructuredDatabaseStatusResponse(
        status="connected",
        backend="pgvector",
        label="文档向量库已连接",
        checked_at=checked_at,
    )


def _corpus_documents() -> list[UnstructuredDocumentInfo]:
    """Return file names and formats only; never read document contents."""
    from app.rag.ingest.loader import discover_corpus_files

    paths, _ignored = discover_corpus_files()
    result = []
    for raw_path in paths:
        path = Path(raw_path)
        result.append(UnstructuredDocumentInfo(
            doc_id=path.stem,
            title=path.stem,
            extension=path.suffix.lstrip(".").lower(),
        ))
    return result


def _loaded_corpus_index(orchestrator: Any) -> Any | None:
    """Read an already-loaded index without triggering the lazy RAG factory."""
    registry = getattr(orchestrator, "registry", None)
    tool = registry.get("doc_search") if registry is not None else None
    rag_tool = getattr(tool, "_rag_tool", None)
    if rag_tool is None:
        return None
    return getattr(rag_tool, "corpus_index", None) or getattr(
        getattr(rag_tool, "_rag", None), "corpus_index", None
    )


def _active_doc_search_backend(orchestrator: Any) -> str:
    """Prefer the backend actually registered in this process over env snapshots."""
    registry = getattr(orchestrator, "registry", None)
    tool = registry.get("doc_search") if registry is not None else None
    if tool is None:
        return (os.environ.get("DOC_SEARCH_BACKEND") or "rag").strip().lower()
    class_name = type(tool).__name__.casefold()
    if "mock" in class_name:
        return "mock"
    if "multihop" in class_name:
        return "rag_multihop"
    return "rag"


def _chunk_counts(chunks: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        doc_id = getattr(chunk, "doc_id", None)
        if isinstance(doc_id, str) and doc_id:
            counts[doc_id] = counts.get(doc_id, 0) + 1
    return counts


def _with_index_counts(
    documents: list[UnstructuredDocumentInfo],
    chunk_counts: dict[str, int] | None,
) -> list[UnstructuredDocumentInfo]:
    if chunk_counts is None:
        return documents
    return [
        document.model_copy(update={"indexed_chunk_count": chunk_counts.get(document.doc_id, 0)})
        for document in documents
    ]


def _inspect_pgvector(
    dsn: str,
) -> tuple[str, str, bool, list[DatabaseTableInfo], dict[str, int]]:
    """Read PostgreSQL identity, pgvector tables, columns, and per-document counts."""
    import psycopg

    with psycopg.connect(
        dsn,
        connect_timeout=3,
        options="-c statement_timeout=3000",
    ) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_schema(), to_regtype('vector') IS NOT NULL")
            identity = cursor.fetchone()
            if not identity:
                raise RuntimeError("database identity query returned no row")
            database, schema_name, vector_ready = identity

            cursor.execute(
                "SELECT table_name, column_name, data_type, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = ANY(%s) "
                "ORDER BY table_name, ordinal_position",
                (schema_name, ["chunk_vectors", "doc_meta"]),
            )
            column_rows = cursor.fetchall()
            cursor.execute(
                "SELECT tc.table_name, kcu.column_name "
                "FROM information_schema.table_constraints AS tc "
                "JOIN information_schema.key_column_usage AS kcu "
                "ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_schema = kcu.table_schema "
                "AND tc.table_name = kcu.table_name "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "AND tc.table_schema = %s AND tc.table_name = ANY(%s)",
                (schema_name, ["chunk_vectors", "doc_meta"]),
            )
            primary_keys = {(table_name, column_name) for table_name, column_name in cursor.fetchall()}
            table_columns: dict[str, list[DatabaseColumnInfo]] = {}
            for table_name, column_name, data_type, udt_name, is_nullable in column_rows:
                display_type = udt_name if data_type == "USER-DEFINED" else data_type
                table_columns.setdefault(table_name, []).append(DatabaseColumnInfo(
                    name=column_name,
                    type=display_type,
                    nullable=is_nullable == "YES",
                    is_primary_key=(table_name, column_name) in primary_keys,
                ))

            tables = [
                DatabaseTableInfo(
                    schema_name=schema_name,
                    name=table_name,
                    description=("文档切片与向量" if table_name == "chunk_vectors" else "文档索引指纹与版本元数据"),
                    column_count=len(columns),
                    columns=columns,
                )
                for table_name, columns in sorted(table_columns.items())
            ]
            chunk_counts: dict[str, int] = {}
            if "chunk_vectors" in table_columns:
                cursor.execute("SELECT doc_id, count(*) FROM chunk_vectors GROUP BY doc_id")
                chunk_counts = {doc_id: count for doc_id, count in cursor.fetchall()}
    return database, schema_name, bool(vector_ready), tables, chunk_counts


def _table_info(table: TableMeta) -> DatabaseTableInfo:
    """Map safe table metadata and omit example values and row counts."""
    primary_keys = {name.casefold() for name in table.primary_key}
    foreign_key_targets: dict[str, str] = {}
    for foreign_key in table.foreign_keys:
        for index, column_name in enumerate(foreign_key.columns):
            if not foreign_key.ref_table:
                continue
            ref_column = (
                foreign_key.ref_columns[index]
                if index < len(foreign_key.ref_columns)
                else ""
            )
            target = f"{foreign_key.ref_table}.{ref_column}" if ref_column else foreign_key.ref_table
            foreign_key_targets[column_name.casefold()] = target

    columns = [
        DatabaseColumnInfo(
            name=column.name,
            type=column.type,
            nullable=column.nullable,
            is_primary_key=(
                column.is_primary_key or column.name.casefold() in primary_keys
            ),
            description=column.description,
            foreign_key_to=foreign_key_targets.get(column.name.casefold()),
        )
        for column in table.columns
    ]
    return DatabaseTableInfo(
        schema_name=table.schema,
        name=table.name,
        description=table.description,
        column_count=len(columns),
        columns=columns,
    )


__all__ = [
    "build_database_schema",
    "build_database_status",
    "build_unstructured_database",
]
