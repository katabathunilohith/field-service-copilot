"""Tribal knowledge: site rules, hazards and equipment quirks that technicians learn on site.

Repair outcomes say *what fixed the fault*. Field notes capture everything else a
technician wishes they had known before arriving: "Tower B roof needs a facilities
escort after 6pm", "the VFD door hinge is seized, bring a 10 mm socket", "rows 6-9
exceed 70 °C by 13:00". Notes are retained to Hindsight like any other memory
(journaled first, delivered in the background, synced by the outbox), and every
later visit to that unit or site starts with a briefing recalled from them.

Notes are shared with every technician, so they are screened before saving:
access codes, passwords and phone numbers are redacted. Those belong in the site's
access system, not in shared memory.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from config import DATA_DIR
from fleet.catalog import Catalog
from memory.bank_schemas import NOTE_KINDS, RepairRecord, now_iso, resolve_bank_id, slug
from memory.hindsight_wrapper import HindsightMemory
from memory.insights import MemoryHit

MIN_NOTE_CHARS = 8
MAX_NOTE_CHARS = 500
NOTE_RETAIN_WAIT_S = 10.0

# A credential keyword followed by a code-like token (contains a digit or symbol).
_CREDENTIAL = re.compile(
    r"\b(pass(?:word|code|phrase)|pin|gate code|door code|alarm code|access code|lock ?box(?: code)?|"
    r"key ?pad(?: code)?|keycode|combination|combo|wi-?fi(?: password)?|login|username)\b"
    r"(\s*(?:is|was|=|:|-|#|no\.?)?\s*)"
    r"((?=[A-Za-z0-9#*@!$%&_-]*[0-9#*@!$%&])[A-Za-z0-9#*@!$%&_-]{3,})",
    re.IGNORECASE,
)
# A bare "code 4471": four or more digits. Equipment codes ("error code 412", "E-412") are left alone.
_BARE_CODE = re.compile(r"(?<!error )(?<!fault )\b(code)(\s*(?:is|was|=|:|#)?\s*)(\d{4,}#?)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\[(?:phone |email )?redacted\]")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?!\w)")  # a sentence may end right after it

_HAZARD_WORDS = re.compile(r"\b(hazard|danger|slip|slippery|live|exposed|asbestos|heat|hot|confined|fall|ladder|"
                           r"forklift|traffic|ice|wasp|bee|snake|arc flash|unguarded|hi-?vis|ppe)\b|\d\s*°\s*C",
                           re.IGNORECASE)
_SITE_WORDS = re.compile(r"\b(escort|access|badge|key|keypad|security|sign in|sign-in|parking|gate|permit|after hours|"
                         r"after \d+|before \d+|reception|induction|loading dock|roof access|closes|facilities|"
                         r"plant room|wi-?fi)\b", re.IGNORECASE)


@dataclass
class Screened:
    text: str
    redactions: list[str]


def screen_note(text: str) -> Screened:
    """Redact credentials and contact details; keep technical detail (sizes, torques, times) intact."""
    redactions: list[str] = []

    def credential(m: re.Match[str]) -> str:
        redactions.append(f"{m.group(1).lower()} value")
        return f"{m.group(1)}{m.group(2)}[redacted]"

    clean = _CREDENTIAL.sub(credential, text)
    clean = _BARE_CODE.sub(credential, clean)

    def phone(m: re.Match[str]) -> str:
        if sum(c.isdigit() for c in m.group(0)) < 10:  # dates, part numbers and measurements stay
            return m.group(0)
        redactions.append("phone number")
        return "[phone redacted]"

    clean = _PHONE.sub(phone, clean)
    if _EMAIL.search(clean):
        redactions.append("email address")
        clean = _EMAIL.sub("[email redacted]", clean)
    return Screened(re.sub(r"\s+", " ", clean).strip(), redactions)


def infer_kind(text: str) -> str:
    if _HAZARD_WORDS.search(text):
        return "hazard"
    if _SITE_WORDS.search(text):
        return "site_rule"
    return "machine_quirk"


def note_record(
    catalog: Catalog, *, text: str, technician_id: str, unit_id: str, scope: str, kind: str,
    occurred_at: str | None = None, record_id: str | None = None,
) -> RepairRecord:
    """Build the retained record for a note. `scope='site'` makes it apply to every unit at the site."""
    unit = catalog.units[unit_id]
    model = catalog.models[unit["model"]]
    site_wide = scope == "site"
    return RepairRecord(
        record_id=record_id or f"note-{secrets.token_hex(5)}",
        record_type="note",
        occurred_at=occurred_at or now_iso(),
        technician_id=technician_id,
        technician_role=catalog.technician_role(technician_id),
        unit_id="" if site_wide else unit_id,
        site=catalog.site_name(unit_id),
        model_key="" if site_wide else unit["model"],
        model_name="" if site_wide else model["name"],
        fleet="" if site_wide else model["fleet"],
        error_code="",
        error_title="",
        action_taken="",
        action_category="unknown",
        outcome_held=None,
        root_cause="",
        notes=text,
        knowledge_source="field_note",
        note_kind=kind,
    )


def seed_notes(catalog: Catalog) -> list[RepairRecord]:
    """Synthetic demo notes (data/field_notes_seed.json): tribal knowledge from the four seeded weeks."""
    path = DATA_DIR / "field_notes_seed.json"
    if not path.exists():
        return []
    return [
        note_record(catalog, text=n["text"], technician_id=n["technician"], unit_id=n["unit"], scope=n["scope"],
                    kind=n["kind"], occurred_at=n["timestamp"], record_id=n["id"])
        for n in json.loads(path.read_text())
    ]


class FieldNotes:
    def __init__(self, memory: HindsightMemory, catalog: Catalog) -> None:
        self.memory = memory
        self.catalog = catalog

    # --------------------------------------------------------------- writing
    async def add(
        self, *, text: str, technician_id: str, unit_id: str, scope: str = "unit", kind: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if unit_id not in self.catalog.units:
            raise ValueError(f"Unknown unit {unit_id}")
        if technician_id not in self.catalog.technicians:
            raise ValueError(f"Unknown technician {technician_id}")
        screened = screen_note(text)
        if len(_PLACEHOLDER.sub("", screened.text).strip(" .,:;-")) < MIN_NOTE_CHARS:
            raise ValueError("Nothing useful is left once access codes and contact details are removed; "
                             "those stay in the site's access system, never in shared memory")
        kind = kind if kind in NOTE_KINDS else infer_kind(screened.text)
        record = note_record(self.catalog, text=screened.text[:MAX_NOTE_CHARS], technician_id=technician_id,
                             unit_id=unit_id, scope="site" if scope == "site" else "unit", kind=kind)
        bank_id = resolve_bank_id(record.fleet or None)
        retain = await self.memory.retain_records(bank_id, [record], run_id=run_id, wait_s=NOTE_RETAIN_WAIT_S)
        return {"note": self._item(record, source="local"), "retain": retain.to_dict(), "redactions": screened.redactions}

    # --------------------------------------------------------------- reading
    def _banks(self, unit_id: str) -> list[str]:
        fleet = self.catalog.models[self.catalog.units[unit_id]["model"]]["fleet"]
        return list(dict.fromkeys([resolve_bank_id(fleet), resolve_bank_id(None)]))  # site-wide notes live in the base bank

    def recall_args(self, unit_id: str) -> tuple[str, list[str], dict[str, Any]]:
        """Shared by briefing() and prefetch so a prefetched briefing is a cache hit."""
        site = self.catalog.site_name(unit_id)
        # Every note carries its site tag, so "all of site + record:note" returns the site's notes
        # (both scopes) without repair facts crowding them out; _applies() then drops quirks
        # pinned to other units at the same site.
        return (
            f"Field notes for {site} and unit {unit_id}: site access rules, hazards and equipment quirks.",
            [f"site:{slug(site)}", "record:note"],
            {"types": ["world", "experience"], "budget": "low", "max_tokens": 1500, "tags_match": "all_strict"},
        )

    def recall_bodies(self, unit_id: str) -> list[tuple[str, dict[str, Any]]]:
        query, tags, kwargs = self.recall_args(unit_id)
        return [(bank, self.memory.recall_body(query, tags, **kwargs)) for bank in self._banks(unit_id)]

    def _applies(self, tags: list[str], metadata: dict[str, str], unit_id: str, site: str) -> bool:
        """A note applies if it is site-wide at this site, or pinned to this very unit."""
        if "record:note" not in tags and metadata.get("record_type") != "note":
            return False
        if f"site:{slug(site)}" not in tags and metadata.get("site") != site:
            return False
        note_unit = metadata.get("unit_id") or next((t.split(":", 1)[1].upper() for t in tags if t.startswith("unit:")), "")
        return not note_unit or note_unit == unit_id

    def _item(self, record: RepairRecord, *, source: str) -> dict[str, Any]:
        return {
            "id": record.record_id,
            "kind": record.note_kind,
            "text": record.notes,
            "technician": record.technician_id,
            "role": record.technician_role,
            "date": record.occurred_at[:10],
            "unit_id": record.unit_id or None,
            "scope": "unit" if record.unit_id else "site",
            "site": record.site,
            "source": source,
        }

    def _hit_item(self, hit: MemoryHit) -> dict[str, Any]:
        m = hit.metadata
        return {
            "id": hit.document_id or hit.id,
            "kind": m.get("note_kind") or next((t.split(":", 1)[1] for t in hit.tags if t.startswith("note:")), "machine_quirk"),
            "text": m.get("note_text") or hit.text,
            "technician": hit.technician,
            "role": m.get("technician_role", ""),
            "date": hit.when,
            "unit_id": m.get("unit_id") or None,
            "scope": "unit" if m.get("unit_id") else "site",
            "site": m.get("site", ""),
            "source": "hindsight",
        }

    def local_notes(self, unit_id: str, *, runtime_only: bool = False) -> list[RepairRecord]:
        site = self.catalog.site_name(unit_id)
        source = self.memory.fallback.runtime_records() if runtime_only else self.memory.fallback.all_records()
        return [r for r in source
                if r.record_type == "note" and r.site == site and (not r.unit_id or r.unit_id == unit_id)]

    async def briefing(self, unit_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        """Everything the fleet has noted about this unit and its site, recalled from Hindsight.

        Notes saved moments ago may still be in Hindsight's extraction queue; they are merged
        from the local journal and marked as syncing, so the next technician sees them at once."""
        if unit_id not in self.catalog.units:
            raise ValueError(f"Unknown unit {unit_id}")
        site = self.catalog.site_name(unit_id)
        start = time.perf_counter()
        query, tags, kwargs = self.recall_args(unit_id)
        recalls = await asyncio.gather(*(
            self.memory.recall_context(bank, query, tags, run_id=run_id, limit=None, label="site briefing", **kwargs)
            for bank in self._banks(unit_id)
        ))
        from_hindsight = all(r.source == "hindsight" for r in recalls)
        items: dict[str, dict[str, Any]] = {}
        if from_hindsight:
            for recall in recalls:
                for hit in recall.hits:
                    key = hit.document_id or hit.id
                    if key not in items and self._applies(hit.tags, hit.metadata, unit_id, site):
                        items[key] = self._hit_item(hit)
        # With Hindsight up, only notes this app saved and Hindsight has not extracted yet are merged
        # in ("syncing"). With it down, the local journal and seed data answer on their own.
        for record in self.local_notes(unit_id, runtime_only=from_hindsight):
            if record.record_id not in items:
                items[record.record_id] = self._item(record, source="syncing" if from_hindsight else "local")

        order = {"hazard": 0, "site_rule": 1, "machine_quirk": 2}
        ranked = sorted(items.values(), key=lambda i: i["date"], reverse=True)
        ranked.sort(key=lambda i: order.get(i["kind"], 3))
        return {
            "unit_id": unit_id,
            "site": site,
            "items": ranked,
            "source": "hindsight" if from_hindsight else "local_fallback",
            "status": ",".join(sorted({r.status for r in recalls})),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


def format_briefing(briefing: dict[str, Any] | None) -> str | None:
    """Compact prompt block. Returns None when there is nothing to say."""
    if not briefing or not briefing.get("items"):
        return None
    lines = []
    for i in briefing["items"][:8]:
        where = f"unit {i['unit_id']}" if i["unit_id"] else "whole site"
        lines.append(f"- [{NOTE_KINDS.get(i['kind'], i['kind'])}, {where}] {i['text']} ({i['technician']}, {i['date']})")
    return "\n".join(lines)
