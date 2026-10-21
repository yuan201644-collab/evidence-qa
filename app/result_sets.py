"""Process-local bounded cache for structured query result pages.

Result handles are unpredictable, short-lived bearer capabilities and are also
bound to the originating chat session to prevent accidental cross-session use.
The session identifier is not an authentication credential.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.contracts import ResultSetInfo, ResultSetPage
from app.nl2sql.config import env_int


@dataclass
class _StoredResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    complete: bool
    session_id: str | None
    expires_at: float
    expires_at_iso: str
    size_bytes: int


class ResultSetStore:
    """A TTL- and size-bounded result cache; handles are opaque bearer tokens."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_sets: int = 16,
        max_rows: int = 5000,
        max_bytes: int = 16 * 1024 * 1024,
        page_size: int = 100,
        clock=time.monotonic,
    ) -> None:
        if ttl_seconds < 1 or max_sets < 1 or max_rows < 1 or max_bytes < 1:
            raise ValueError("结果集缓存限制必须为正数")
        if page_size < 1 or page_size > 200:
            raise ValueError("结果页大小必须在 1 到 200 之间")
        self.ttl_seconds = ttl_seconds
        self.max_sets = max_sets
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.page_size = page_size
        self._clock = clock
        self._items: OrderedDict[str, _StoredResult] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "ResultSetStore":
        return cls(
            ttl_seconds=env_int("RESULT_SET_TTL_SECONDS", 600, minimum=60),
            max_sets=env_int("RESULT_SET_CACHE_MAX_SETS", 16, minimum=1),
            max_rows=env_int("SQL_LARGE_RESULT_MAX_ROWS", 5000, minimum=1),
            max_bytes=env_int("RESULT_SET_CACHE_MAX_BYTES", 16 * 1024 * 1024, minimum=1024),
            page_size=env_int("RESULT_SET_PAGE_SIZE", 100, minimum=1),
        )

    def put(
        self,
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        complete: bool,
        session_id: str | None = None,
        min_rows: int | None = None,
    ) -> ResultSetInfo | None:
        """缓存一份结果集，返回可翻页的句柄。

        `min_rows` 是**入库下限**，默认等于 `page_size`（即只收超过一页的结果）。
        为什么要有它：答案文字只预览前几行（见 `nl2sql.pipeline.build_conclusion`
        的 `max_preview`），如果入库门槛仍是一整页，那么「预览行数 < 行数 <= page_size」
        这段区间里的结果会被截断却又不入库 —— 用户看着「共 26 行，前 3 行：…」，
        剩下的 23 行在界面上**无处可查**，省略号后面没有去处。
        调用方（编排层）用同一个预览阈值当 `min_rows`，两者就不会脱节。
        """
        threshold = self.page_size if min_rows is None else min(min_rows, self.page_size)
        if not columns or len(rows) <= threshold:
            return None

        kept_rows: list[dict[str, Any]] = []
        byte_count = len(json.dumps(columns, ensure_ascii=False).encode("utf-8"))
        for row in rows[: self.max_rows]:
            row_size = len(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            )
            if byte_count + row_size > self.max_bytes:
                complete = False
                break
            kept_rows.append(dict(row))
            byte_count += row_size
        if len(kept_rows) < len(rows):
            complete = False
        if len(kept_rows) <= threshold:
            return None

        now = self._clock()
        expires = now + self.ttl_seconds
        expires_iso = datetime.fromtimestamp(time.time() + self.ttl_seconds, timezone.utc).isoformat()
        record = _StoredResult(
            columns=list(columns),
            rows=kept_rows,
            complete=bool(complete),
            session_id=session_id,
            expires_at=expires,
            expires_at_iso=expires_iso,
            size_bytes=byte_count,
        )
        if byte_count > self.max_bytes:
            return None
        with self._lock:
            self._purge_expired(now)
            while self._items and (
                len(self._items) >= self.max_sets
                or self._total_bytes + byte_count > self.max_bytes
            ):
                _, evicted = self._items.popitem(last=False)
                self._total_bytes -= evicted.size_bytes
            result_id = secrets.token_urlsafe(32)
            self._items[result_id] = record
            self._total_bytes += byte_count
        return ResultSetInfo(
            id=result_id,
            row_count=len(kept_rows),
            complete=record.complete,
            page_size=self.page_size,
            expires_at=expires_iso,
        )

    def page(
        self,
        result_id: str,
        *,
        offset: int = 0,
        page_size: int | None = None,
        session_id: str | None = None,
    ) -> ResultSetPage | None:
        requested_size = self.page_size if page_size is None else page_size
        if offset < 0 or requested_size < 1 or requested_size > 200:
            raise ValueError("offset 或 page_size 超出允许范围")
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            record = self._items.get(result_id)
            if record is None or record.session_id != session_id:
                return None
            self._items.move_to_end(result_id)
            page_rows = record.rows[offset : offset + requested_size]
            next_offset = offset + len(page_rows)
            if next_offset >= len(record.rows):
                next_offset = None
            return ResultSetPage(
                id=result_id,
                columns=list(record.columns),
                rows=[dict(row) for row in page_rows],
                offset=offset,
                page_size=requested_size,
                row_count=len(record.rows),
                complete=record.complete,
                next_offset=next_offset,
                expires_at=record.expires_at_iso,
            )

    def csv_bytes(self, result_id: str, *, session_id: str | None = None) -> bytes | None:
        with self._lock:
            self._purge_expired(self._clock())
            record = self._items.get(result_id)
            if record is None or record.session_id != session_id or not record.complete:
                return None
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\r\n")
            writer.writerow(record.columns)
            for row in record.rows:
                writer.writerow([_safe_csv_value(row.get(column)) for column in record.columns])
            return output.getvalue().encode("utf-8-sig")

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            removed = self._items.pop(key)
            self._total_bytes -= removed.size_bytes


def _safe_csv_value(value: Any) -> Any:
    """Neutralize spreadsheet formula prefixes in text cells, preserving numbers."""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


__all__ = ["ResultSetStore"]
