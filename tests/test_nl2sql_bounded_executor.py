from types import SimpleNamespace

from app.nl2sql import executor as executor_module


class _Description:
    def __init__(self, name: str) -> None:
        self.name = name


class _Cursor:
    def __init__(self, *, named: bool, rows: list[dict]) -> None:
        self.named = named
        self.rows = list(rows)
        self.position = 0
        self.description = None
        self.executed: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))
        if sql.startswith("SELECT set_config"):
            return
        self.description = [_Description("id")]

    def fetchall(self):
        return self.rows

    def fetchmany(self, size: int):
        batch = self.rows[self.position : self.position + size]
        self.position += len(batch)
        return batch


class _Connection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.read_only = False
        self.named_cursor_used = False
        self.rollback_called = False
        self.closed = False

    def cursor(self, *, name=None, row_factory=None):
        if name is None:
            return _Cursor(named=False, rows=[])
        self.named_cursor_used = True
        return _Cursor(named=True, rows=self.rows)

    def rollback(self):
        self.rollback_called = True

    def close(self):
        self.closed = True


class _Psycopg:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self, *_args, **_kwargs):
        return self.connection


def _executor(monkeypatch, rows: list[dict]):
    connection = _Connection(rows)
    psycopg = _Psycopg(connection)
    monkeypatch.setattr(
        executor_module,
        "_require_psycopg",
        lambda: (psycopg, object()),
    )
    return executor_module.SqlExecutor(dsn="postgresql://test", readonly=True), connection


def test_bounded_execution_uses_server_cursor_and_marks_row_cap(monkeypatch):
    executor, connection = _executor(monkeypatch, [{"id": value} for value in range(6)])

    result = executor.execute(
        "SELECT id FROM sample LIMIT 7",
        max_result_rows=3,
        max_result_bytes=4096,
    )

    assert connection.named_cursor_used is True
    assert connection.read_only is True
    assert connection.rollback_called is True
    assert connection.closed is True
    assert [row["id"] for row in result.rows] == [0, 1, 2]
    assert result.row_count == 3
    assert result.result_truncated is True


def test_bounded_execution_marks_byte_cap_without_returning_over_budget_row(monkeypatch):
    executor, _connection = _executor(monkeypatch, [{"id": "a"}, {"id": "long-value"}])

    result = executor.execute(
        "SELECT id FROM sample LIMIT 3",
        max_result_rows=3,
        max_result_bytes=12,
    )

    assert result.rows == [{"id": "a"}]
    assert result.result_truncated is True
