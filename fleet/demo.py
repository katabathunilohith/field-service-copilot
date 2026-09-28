"""Demo checkpoint and reset.

Every rehearsal of the live demo retains new notes, sessions and outcomes, so the
"empty briefing, then learned" moment only works once. A checkpoint snapshots the
app's local records; a reset deletes from Hindsight exactly what this app created
since then (plus bulletins approved since, and their scoped directives) and restores
the local state. Seeded history and the bank's base directives are never touched.

Without a saved checkpoint, a reset returns to the seeded data only.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fleet.catalog import Catalog
from memory.bank_schemas import RepairRecord, load_seed, now_iso, records_from_seed, resolve_bank_id
from memory.field_notes import seed_notes
from memory.hindsight_wrapper import HindsightError, HindsightMemory

RUNTIME_FILES = ("memory_journal.jsonl", "retain_acks.jsonl", "tickets.jsonl", "bulletins.jsonl")


class DemoResetError(Exception):
    pass


def _journal(path: Path) -> dict[str, RepairRecord]:
    records: dict[str, RepairRecord] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                record = RepairRecord.from_dict(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
            records.setdefault(record.record_id, record)
    return records


def _approved_bulletins(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                bulletin = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest[bulletin["id"]] = bulletin
    return {bid: b for bid, b in latest.items() if b.get("status") == "approved"}


def _tickets(path: Path) -> set[str]:
    ids: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                ids.add(json.loads(line)["ticket"]["id"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return ids


class DemoReset:
    def __init__(self, runtime_dir: Path, catalog: Catalog, memory: HindsightMemory) -> None:
        self.runtime = runtime_dir
        self.snapshot = runtime_dir / "demo_checkpoint"
        self.catalog = catalog
        self.memory = memory
        self._seed_ids = {r.record_id for r in records_from_seed(load_seed()) + seed_notes(catalog)}

    # ------------------------------------------------------------ checkpoint
    def checkpoint_info(self) -> dict[str, Any] | None:
        meta = self.snapshot / "checkpoint.json"
        return json.loads(meta.read_text()) if meta.exists() else None

    def save_checkpoint(self, label: str = "") -> dict[str, Any]:
        """Snapshot the current local records. Later resets return to exactly this state."""
        if self.snapshot.exists():
            shutil.rmtree(self.snapshot)
        self.snapshot.mkdir(parents=True)
        for name in RUNTIME_FILES:
            if (self.runtime / name).exists():
                shutil.copy2(self.runtime / name, self.snapshot / name)
        info = {
            "created_at": now_iso(),
            "label": label.strip()[:80],
            "records": len(_journal(self.snapshot / "memory_journal.jsonl")),
            "tickets": len(_tickets(self.snapshot / "tickets.jsonl")),
            "approved_bulletins": len(_approved_bulletins(self.snapshot / "bulletins.jsonl")),
        }
        (self.snapshot / "checkpoint.json").write_text(json.dumps(info, indent=2))
        return info

    # --------------------------------------------------------------- preview
    def preview(self) -> dict[str, Any]:
        """Exactly what a reset would delete, so the UI can ask before doing it."""
        base_dir = self.snapshot if self.checkpoint_info() else None
        current = _journal(self.runtime / "memory_journal.jsonl")
        kept = _journal(base_dir / "memory_journal.jsonl") if base_dir else {}
        doomed = [r for rid, r in current.items() if rid not in kept]
        overlap = {r.record_id for r in doomed} & self._seed_ids
        if overlap:  # the journal only ever holds app-created records; refuse if that assumption breaks
            raise DemoResetError(f"refusing to delete seeded records: {sorted(overlap)[:3]}")

        approved_now = _approved_bulletins(self.runtime / "bulletins.jsonl")
        approved_base = _approved_bulletins(base_dir / "bulletins.jsonl") if base_dir else {}
        bulletins = [b for bid, b in approved_now.items() if bid not in approved_base]
        tickets = _tickets(self.runtime / "tickets.jsonl") - (_tickets(base_dir / "tickets.jsonl") if base_dir else set())

        def count(kind: str) -> int:
            return sum(r.record_type == kind for r in doomed)

        return {
            "base": {"kind": "checkpoint", **self.checkpoint_info()} if base_dir else {"kind": "seed"},
            "documents": [{"id": r.document_id, "type": r.record_type, "unit_id": r.unit_id or None,
                           "technician": r.technician_id, "at": r.occurred_at} for r in doomed],
            "counts": {
                "notes": count("note"),
                "sessions": count("diagnosis"),
                "outcomes": count("outcome"),
                "bulletins": len(bulletins),
                "directives": len(bulletins),
                "tickets": len(tickets),
            },
            "bulletins": [b["id"] for b in bulletins],
            "nothing_to_do": not doomed and not bulletins and not tickets,
        }

    # ----------------------------------------------------------------- reset
    async def reset(self) -> dict[str, Any]:
        """Delete what the preview lists from Hindsight, then restore the local records.

        Deletes are idempotent (an already-deleted document counts as done). If any delete
        fails, local state is left untouched so a retry can finish the job."""
        if not await self.memory.wait_for_deliveries(30.0):
            raise DemoResetError("a retain is still being delivered; try again in a few seconds")
        plan = self.preview()
        deleted_docs: list[str] = []
        deleted_directives: list[str] = []
        failed: list[str] = []
        if self.memory.enabled:
            journal = _journal(self.runtime / "memory_journal.jsonl")
            for doc in plan["documents"]:
                record = journal[doc["id"]]
                try:
                    await self.memory.delete_document(resolve_bank_id(record.fleet or None), doc["id"])
                    deleted_docs.append(doc["id"])
                except (TimeoutError, HindsightError) as exc:
                    failed.append(f"{doc['id']}: {str(exc)[:120]}")
            approved = _approved_bulletins(self.runtime / "bulletins.jsonl")
            for bid in plan["bulletins"]:
                fleet = self.catalog.models[approved[bid]["model_key"]]["fleet"]
                bank = resolve_bank_id(fleet)
                try:
                    await self.memory.delete_document(bank, bid.lower())
                    deleted_docs.append(bid.lower())
                    if await self.memory.delete_directive(bank, f"bulletin-{bid.lower()}"):
                        deleted_directives.append(f"bulletin-{bid.lower()}")
                except (TimeoutError, HindsightError) as exc:
                    failed.append(f"{bid}: {str(exc)[:120]}")
        if failed:
            raise DemoResetError("Some Hindsight deletes failed; nothing local was changed. Retry to finish: "
                                 + "; ".join(failed[:3]))

        base_dir = self.snapshot if plan["base"]["kind"] == "checkpoint" else None
        for name in RUNTIME_FILES:
            target = self.runtime / name
            if base_dir and (base_dir / name).exists():
                shutil.copy2(base_dir / name, target)
            elif target.exists():
                target.unlink()
        return {
            "restored_to": plan["base"],
            "deleted_documents": deleted_docs,
            "deleted_directives": deleted_directives,
            "counts": plan["counts"],
        }
