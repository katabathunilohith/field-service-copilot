"""Acknowledgement ledger for the retain outbox.

Every record the app retains is written to the local journal first. A record is
"synced" once Hindsight has accepted it; its document_id is then appended here.
Anything in the journal without an ack is pending and gets pushed by the sync
loop, including sessions recorded while Hindsight was unconfigured or down.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from memory.bank_schemas import now_iso


class AckStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._acked: set[str] = set()
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    self._acked.update(json.loads(line).get("document_ids", []))
                except json.JSONDecodeError:
                    continue

    def add(self, document_ids: list[str], *, bank_id: str, operation_id: str | None) -> None:
        new = [d for d in document_ids if d not in self._acked]
        if not new:
            return
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as fh:
                fh.write(json.dumps({"at": now_iso(), "bank_id": bank_id, "operation_id": operation_id, "document_ids": new}) + "\n")
            self._acked.update(new)

    def __contains__(self, document_id: str) -> bool:
        return document_id in self._acked
