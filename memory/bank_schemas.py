"""Hindsight bank definitions plus the tag, entity and metadata conventions every
retained record follows.

Conventions matter because recall filters on tags, and the Copilot computes
"did the manual fix hold?" statistics from the tags/metadata that come back on
recalled facts. Anything that writes to a bank should build its payload through
`RepairRecord.to_retain_item()` so the conventions stay in one place.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal

from config import DATA_DIR, settings

FLEETS = ("chillers", "solar", "elevators")

RecordType = Literal["repair", "diagnosis", "outcome", "note"]

# Field notes: tribal knowledge that is not a repair outcome.
NOTE_KINDS = {"site_rule": "site rule", "hazard": "hazard", "machine_quirk": "equipment quirk"}


# --------------------------------------------------------------------------- banks
@dataclass(frozen=True)
class Directive:
    """A standing rule Hindsight injects into every reflect on the bank."""

    name: str
    content: str
    priority: int

    def payload(self) -> dict[str, Any]:
        return {"name": self.name, "content": self.content, "priority": self.priority, "is_active": True}


@dataclass(frozen=True)
class BankSchema:
    bank_id: str
    name: str
    mission: str
    background: str
    retain_mission: str
    reflect_mission: str
    observations_mission: str
    directives: tuple[Directive, ...] = ()

    def create_payload(self) -> dict[str, Any]:
        """Body for PUT /v1/default/banks/{bank_id} (create, or no-op update)."""
        return {"background": self.background}

    def config_updates(self) -> dict[str, Any]:
        """Body['updates'] for PATCH /v1/default/banks/{bank_id}/config."""
        return {
            "reflect_mission": self.reflect_mission,
            "retain_mission": self.retain_mission,
            "enable_observations": True,
            "observations_mission": self.observations_mission,
        }


_MISSION = (
    "Help field technicians diagnose and repair commercial HVAC chillers, solar inverters and elevator "
    "hoists faster than the OEM manual alone, by remembering which fixes actually held in the field, "
    "who discovered them, and under which site conditions."
)
_BACKGROUND = (
    "I am the Field Service Copilot for a regional commercial service fleet. I work alongside senior, "
    "specialist and junior technicians. OEM manuals are my baseline; field-verified findings from any "
    "technician outrank a manual step that has repeatedly failed on the same model and error code."
)
_RETAIN_MISSION = (
    "Extract: equipment model, unit ID, site, error code, technician, action taken, whether it was an OEM "
    "manual step or a field-discovered fix, root cause, and whether the fix HELD or did NOT hold. Keep "
    "measurements (ohms, amps, °C, torque) verbatim. Treat technician repair reports as world facts and "
    "the Copilot's own recommendations and their confirmed outcomes as experience."
)
_REFLECT_MISSION = (
    "Synthesize diagnostic guidance for the next technician: which OEM steps keep failing for this "
    "model and error, which field fixes held and how often, who found them and when, and any site or "
    "environmental conditions that change the root cause. Be concrete and cite dates and technicians."
)


_OBSERVATIONS_MISSION = (
    "Consolidate repeated repair outcomes into durable patterns per equipment model and error code: which OEM "
    "manual steps keep failing, which field-discovered fixes hold and how often, who first found them, and any "
    "site or environmental variants (e.g. coastal vs desert). Keep hold counts, dates and technician names."
)

# Safety and evidence rules applied to every reflect on the bank (Hindsight directives).
DIRECTIVES: tuple[Directive, ...] = (
    Directive(
        "safety-first",
        "Always lead with the applicable lockout/tagout or hoistway-entry safety step. Never recommend bypassing "
        "interlocks, safety circuits, ground-fault protection or other protective devices, even temporarily.",
        100,
    ),
    Directive(
        "cite-field-evidence",
        "When guidance relies on field memory, cite the technician, date and unit, and state how many times the "
        "fix held versus did not hold.",
        90,
    ),
    Directive(
        "manual-as-fallback",
        "Recommend a field-verified fix ahead of the OEM procedure only when memory shows it held more often, and "
        "always keep the OEM procedure as the documented fallback.",
        80,
    ),
    Directive(
        "surface-site-constraints",
        "When technicians' field notes record hazards or site access rules for the site or unit, state them before "
        "any procedure steps, crediting who noted them.",
        75,
    ),
    Directive(
        "no-invented-data",
        "Never invent part numbers, torque values, measurements or hold rates that are not present in memory.",
        70,
    ),
)


def bank_schema(bank_id: str, fleet: str | None = None) -> BankSchema:
    scope = f" ({fleet})" if fleet else ""
    return BankSchema(
        bank_id=bank_id,
        name=f"Field Service Copilot{scope}",
        mission=_MISSION,
        background=f"{_MISSION} {_BACKGROUND}",
        retain_mission=_RETAIN_MISSION,
        reflect_mission=_REFLECT_MISSION,
        observations_mission=_OBSERVATIONS_MISSION,
        directives=DIRECTIVES,
    )


def resolve_bank_id(fleet: str | None = None, *, base: str | None = None, per_fleet: bool | None = None) -> str:
    """The bank a record for `fleet` lives in. One shared bank by default; with
    HINDSIGHT_PER_FLEET_BANKS=true each fleet gets `<base>-<fleet>`."""
    base = base or settings.hindsight_bank_id
    per_fleet = settings.hindsight_per_fleet_banks if per_fleet is None else per_fleet
    if per_fleet and fleet in FLEETS:
        return f"{base}-{fleet}"
    return base


def all_bank_ids(*, base: str | None = None, per_fleet: bool | None = None) -> list[str]:
    per_fleet = settings.hindsight_per_fleet_banks if per_fleet is None else per_fleet
    if per_fleet:
        return [resolve_bank_id(f, base=base, per_fleet=True) for f in FLEETS]
    return [resolve_bank_id(None, base=base, per_fleet=False)]


# ---------------------------------------------------------------------- tags
def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def model_tag(model_key: str) -> str:
    return f"model:{model_key}"


def error_tag(error_code: str) -> str:
    return f"error:{slug(error_code)}"


def unit_tag(unit_id: str) -> str:
    return f"unit:{slug(unit_id)}"


def tech_tag(technician_id: str) -> str:
    return f"tech:{slug(technician_id)}"


def context_tags(model_key: str | None, error_code: str | None, unit_id: str | None = None) -> list[str]:
    """Tags used to scope a recall to the equipment under diagnosis."""
    tags = []
    if model_key:
        tags.append(model_tag(model_key))
    if error_code:
        tags.append(error_tag(error_code))
    if unit_id:
        tags.append(unit_tag(unit_id))
    return tags


def outcome_label(outcome_held: bool | None) -> str:
    return "pending" if outcome_held is None else ("held" if outcome_held else "failed")


def parse_outcome(value: str | None) -> bool | None:
    if value in ("true", "held"):
        return True
    if value in ("false", "failed"):
        return False
    return None


# ------------------------------------------------------------------- records
@dataclass
class RepairRecord:
    """One retained memory: a repair visit, a Copilot diagnosis, an outcome confirmation, or a
    technician's field note (a site rule, hazard or equipment quirk; see memory/field_notes.py)."""

    record_id: str
    record_type: RecordType
    occurred_at: str
    technician_id: str
    technician_role: str
    unit_id: str
    site: str
    model_key: str
    model_name: str
    fleet: str
    error_code: str
    error_title: str
    action_taken: str
    action_category: Literal["manual", "field", "unknown"]
    outcome_held: bool | None
    root_cause: str = "unconfirmed"
    notes: str = ""
    work_order: str = ""
    visit: int = 1
    knowledge_source: str = ""
    learned_from_technician: str = ""
    extra_tags: list[str] = field(default_factory=list)
    note_kind: str = ""  # field notes only: site_rule | hazard | machine_quirk

    # ---- derived views -------------------------------------------------------
    @property
    def tags(self) -> list[str]:
        if self.record_type == "note":
            return self._note_tags()
        tags = [
            model_tag(self.model_key),
            error_tag(self.error_code),
            unit_tag(self.unit_id),
            tech_tag(self.technician_id),
            f"fleet:{self.fleet}",
            f"site:{slug(self.site)}",
            f"record:{self.record_type}",
            f"procedure:{self.action_category}",
            f"outcome:{outcome_label(self.outcome_held)}",
        ]
        return tags + [t for t in self.extra_tags if t not in tags]

    def _note_tags(self) -> list[str]:
        # A site-wide note has no unit tag, so it surfaces for every unit at that site.
        tags = [f"site:{slug(self.site)}", "record:note", f"note:{self.note_kind}", tech_tag(self.technician_id)]
        if self.unit_id:
            tags.append(unit_tag(self.unit_id))
        if self.model_key:
            tags.append(model_tag(self.model_key))
        if self.fleet:
            tags.append(f"fleet:{self.fleet}")
        return tags + [t for t in self.extra_tags if t not in tags]

    @property
    def metadata(self) -> dict[str, str]:
        # Hindsight metadata values must be strings.
        if self.record_type == "note":
            return {
                "record_type": "note",
                "note_kind": self.note_kind,
                "note_text": self.notes,
                "technician": self.technician_id,
                "technician_role": self.technician_role,
                "unit_id": self.unit_id,
                "site": self.site,
                "model": self.model_key,
                "occurred_at": self.occurred_at,
            }
        return {
            "record_type": self.record_type,
            "work_order": self.work_order,
            "visit": str(self.visit),
            "technician": self.technician_id,
            "technician_role": self.technician_role,
            "unit_id": self.unit_id,
            "site": self.site,
            "model": self.model_key,
            "model_name": self.model_name,
            "error_code": self.error_code,
            "action_taken": self.action_taken,
            "action_category": self.action_category,
            "root_cause": self.root_cause,
            "outcome_held": {True: "true", False: "false", None: "pending"}[self.outcome_held],
            "knowledge_source": self.knowledge_source,
            "learned_from_technician": self.learned_from_technician,
            "occurred_at": self.occurred_at,
        }

    @property
    def entities(self) -> list[dict[str, str]]:
        entities = [
            {"text": self.technician_id, "type": "technician"},
            {"text": self.unit_id, "type": "equipment_unit"},
            {"text": self.model_name, "type": "equipment_model"},
            {"text": self.error_code, "type": "error_code"},
            {"text": self.site, "type": "site"},
        ]
        return [e for e in entities if e["text"]]  # site-wide notes have no unit, model or code

    @property
    def content(self) -> str:
        """Narrative first (for fact extraction), structured summary last (for grounding)."""
        if self.record_type == "note":
            where = f"{self.site}, unit {self.unit_id}" + (f" ({self.model_name})" if self.model_name else "") if self.unit_id \
                else f"{self.site} (applies to the whole site)"
            kind = NOTE_KINDS.get(self.note_kind, "field note")
            return (f"[Field note · {kind} · {self.occurred_at[:10]}] At {where}: {self.notes} "
                    f"Reported by {self.technician_id} ({self.technician_role}) on {self.occurred_at[:10]}.")
        label = {"repair": "Field repair record", "diagnosis": "Copilot diagnosis session", "outcome": "Fix outcome confirmation"}[
            self.record_type
        ]
        ref = f" {self.work_order} visit {self.visit}" if self.work_order else ""
        outcome = {True: "HELD", False: "DID NOT HOLD", None: "PENDING field confirmation"}[self.outcome_held]
        summary = (
            f"Summary: technician={self.technician_id} ({self.technician_role}); unit={self.unit_id}; site={self.site}; "
            f"model={self.model_name}; error={self.error_code} ({self.error_title}); action={self.action_taken}; "
            f"procedure={'OEM manual' if self.action_category == 'manual' else 'field-discovered' if self.action_category == 'field' else 'unspecified'}; "
            f"outcome={outcome}; root cause={self.root_cause}."
        )
        return f"[{label}{ref} · {self.occurred_at[:10]}] {self.notes} {summary}".strip()

    @property
    def document_id(self) -> str:
        return self.record_id

    def to_retain_item(self) -> dict[str, Any]:
        """A MemoryItem for POST /v1/default/banks/{bank_id}/memories."""
        return {
            "content": self.content,
            "timestamp": self.occurred_at,
            "context": f"field service {self.record_type} record",
            "document_id": self.document_id,
            "metadata": self.metadata,
            "tags": self.tags,
            "entities": self.entities,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepairRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------- seed conversion
@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    return json.loads((DATA_DIR / "equipment_catalog.json").read_text())


def records_from_seed(seed: dict[str, Any]) -> list[RepairRecord]:
    """Flatten seed work orders into one RepairRecord per technician visit."""
    roles = {t["id"]: t["role"] for t in _catalog()["technicians"]}
    records: list[RepairRecord] = []
    for wo in seed["work_orders"]:
        for v in wo["visits"]:
            learned = v.get("learned_from") or {}
            records.append(
                RepairRecord(
                    record_id=f"{wo['id'].lower()}-v{v['visit']}",
                    record_type="repair",
                    occurred_at=v["timestamp"],
                    technician_id=v["technician"],
                    technician_role=roles.get(v["technician"], ""),
                    unit_id=wo["unit_id"],
                    site=wo["site"],
                    model_key=wo["model"],
                    model_name=wo["model_name"],
                    fleet=wo["fleet"],
                    error_code=wo["error_code"],
                    error_title=wo["error_title"],
                    action_taken=v["action"],
                    action_category=v["action_category"],
                    outcome_held=v["outcome_held"],
                    root_cause=v["root_cause"],
                    notes=v["notes"],
                    work_order=wo["id"],
                    visit=v["visit"],
                    knowledge_source=v.get("knowledge_source", ""),
                    learned_from_technician=learned.get("technician", ""),
                )
            )
    return records


def load_seed() -> dict[str, Any]:
    return json.loads((DATA_DIR / "seed_history.json").read_text())
