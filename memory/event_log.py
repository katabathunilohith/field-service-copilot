"""In-process ring buffer of memory operations, surfaced by the Memory Inspector.

Each event keeps the exact request body sent to Hindsight (never headers, so no
credentials) and a trimmed copy of the response.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from memory.bank_schemas import now_iso

Operation = Literal["recall", "retain", "reflect", "bank"]
Status = Literal["ok", "queued", "fallback", "timeout", "error", "skipped", "deferred"]

_MAX_STR = 4000
_MAX_LIST = 40


def _trim(value: Any, depth: int = 0) -> Any:
    """Keep payloads inspectable without letting one huge response flood the UI."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, list):
        items = [_trim(v, depth + 1) for v in value[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            items.append(f"… {len(value) - _MAX_LIST} more")
        return items
    if isinstance(value, dict):
        return {k: _trim(v, depth + 1) for k, v in value.items()}
    return value


@dataclass
class MemoryEvent:
    op: Operation
    bank_id: str
    status: Status
    source: Literal["hindsight", "local_fallback", "none"]
    latency_ms: int
    request: dict[str, Any] = field(default_factory=dict)
    response: Any = None
    error: str | None = None
    run_id: str | None = None
    summary: str = ""
    id: int = 0
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventLog:
    def __init__(self, maxlen: int = 400) -> None:
        self._events: deque[MemoryEvent] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, event: MemoryEvent) -> MemoryEvent:
        event.request = _trim(event.request)
        event.response = _trim(event.response)
        with self._lock:
            event.id = next(self._ids)
            self._events.append(event)
        return event

    def update(self, event_id: int, **changes: Any) -> None:
        with self._lock:
            for event in self._events:
                if event.id == event_id:
                    for key, value in changes.items():
                        setattr(event, key, _trim(value) if key in ("request", "response") else value)
                    return

    def list(self, *, limit: int = 100, op: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        if op:
            events = [e for e in events if e.op == op]
        if run_id:
            events = [e for e in events if e.run_id == run_id]
        return [e.to_dict() for e in reversed(events[-limit:])]
