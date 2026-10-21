"""Request-scoped, non-contractual progress events for the interactive UI."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, Iterator


ProgressCallback = Callable[[dict[str, object]], None]


@dataclass
class _ProgressContext:
    callback: ProgressCallback
    sequence: int = 0
    active: dict[str, tuple[str, str]] = field(default_factory=dict)


_current: ContextVar[_ProgressContext | None] = ContextVar("ask_progress", default=None)


@contextmanager
def progress_scope(callback: ProgressCallback | None) -> Iterator[None]:
    """Set the progress sink for the current request and restore it afterwards."""
    token = _current.set(_ProgressContext(callback) if callback is not None else None)
    try:
        yield
    finally:
        _current.reset(token)


def emit_progress(
    stage_id: str,
    label: str,
    status: str,
    *,
    elapsed_ms: float | None = None,
) -> None:
    """Publish safe UI metadata only; never include questions, SQL, or source data."""
    context = _current.get()
    if context is None:
        return
    if status == "started" or stage_id not in context.active:
        context.sequence += 1
        event_id = f"stage-{context.sequence}"
        if status == "started":
            context.active[stage_id] = (event_id, label)
    else:
        active = context.active.pop(stage_id, None)
        if active is None:
            context.sequence += 1
            event_id = f"stage-{context.sequence}"
        else:
            event_id = active[0]

    event: dict[str, object] = {
        "type": "stage",
        "id": event_id,
        "stage_id": stage_id,
        "label": label,
        "status": status,
    }
    if elapsed_ms is not None:
        event["elapsed_ms"] = round(max(0.0, elapsed_ms), 1)
    try:
        context.callback(event)
    except Exception:
        # UI progress must not become a failure path for business processing.
        pass


def fail_active_progress() -> None:
    """Close nested stages if a tool exits exceptionally, avoiding stale UI state."""
    context = _current.get()
    if context is None:
        return
    active = list(context.active.items())
    context.active.clear()
    for stage_id, (event_id, label) in active:
        try:
            context.callback({
                "type": "stage",
                "id": event_id,
                "stage_id": stage_id,
                "label": label,
                "status": "failed",
            })
        except Exception:
            pass


__all__ = ["ProgressCallback", "emit_progress", "fail_active_progress", "progress_scope"]
