"""Append-only repair ticket ledger (a minimal CMMS stand-in) persisted as JSONL."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.bank_schemas import now_iso


class TicketLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._tickets: dict[str, dict[str, Any]] = {}
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = entry.get("ticket")
            if ticket and ticket.get("id"):
                self._tickets[ticket["id"]] = ticket

    def _persist(self, event: str, ticket: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(json.dumps({"event": event, "at": now_iso(), "ticket": ticket}) + "\n")

    def create(self, **fields: Any) -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        ticket = {
            "id": f"TKT-{stamp}-{secrets.token_hex(2).upper()}",
            "created_at": now_iso(),
            "status": "open",
            "outcome_held": None,
            "outcome_notes": None,
            "outcome_confirmed_at": None,
            **fields,
        }
        with self._lock:
            self._tickets[ticket["id"]] = ticket
            self._persist("created", ticket)
        return ticket

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        return self._tickets.get(ticket_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return sorted(self._tickets.values(), key=lambda t: t["created_at"], reverse=True)[:limit]

    def confirm_outcome(self, ticket_id: str, *, outcome_held: bool, notes: str, confirmed_by: str) -> dict[str, Any]:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.update(
                status="resolved" if outcome_held else "reopened",
                outcome_held=outcome_held,
                outcome_notes=notes,
                outcome_confirmed_by=confirmed_by,
                outcome_confirmed_at=now_iso(),
            )
            self._persist("outcome", ticket)
            return ticket
