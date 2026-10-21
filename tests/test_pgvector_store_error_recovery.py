"""PostgreSQL vector-store read failures must not poison its reused connection."""

import pytest

from app.rag.retrieval.pgvector_store import PgVectorStore


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, _params):
        raise RuntimeError("simulated SQL failure")


class _Connection:
    def __init__(self):
        self.rolled_back = False

    def cursor(self):
        return _Cursor()

    def rollback(self):
        self.rolled_back = True


def test_search_rolls_back_failed_read_transaction_and_reraises() -> None:
    store = PgVectorStore.__new__(PgVectorStore)
    store._conn = _Connection()

    with pytest.raises(RuntimeError, match="simulated SQL failure"):
        store.search([0.0], top_k=1)

    assert store._conn.rolled_back is True
