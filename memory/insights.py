"""Turn recalled memories into hard numbers: which fixes held, how often, who found them.

Works identically on Hindsight recall results and on local-fallback hits, which
is what lets the agent degrade gracefully without changing its reasoning.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from memory.bank_schemas import RepairRecord, parse_outcome

NO_DEFECT_PREFIX = "inspected recalled field finding"


@dataclass
class MemoryHit:
    id: str
    text: str
    type: str
    score: float
    source: str
    occurred_at: str | None = None
    document_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    via: str = ""  # which recall surfaced it (fleet history, unit history, ...)

    # --- helpers that prefer metadata and fall back to tag conventions -------
    def _tag(self, prefix: str) -> str | None:
        for tag in self.tags:
            if tag.startswith(prefix + ":"):
                return tag.split(":", 1)[1]
        return None

    @property
    def technician(self) -> str | None:
        return self.metadata.get("technician") or _untag_tech(self._tag("tech"))

    @property
    def unit_id(self) -> str | None:
        return self.metadata.get("unit_id") or (self._tag("unit") or "").upper() or None

    @property
    def error_code(self) -> str | None:
        return self.metadata.get("error_code") or (self._tag("error") or "").upper() or None

    @property
    def model(self) -> str | None:
        return self.metadata.get("model") or self._tag("model")

    @property
    def record_type(self) -> str | None:
        return self.metadata.get("record_type") or self._tag("record")

    @property
    def action_category(self) -> str | None:
        return self.metadata.get("action_category") or self._tag("procedure")

    @property
    def action_taken(self) -> str | None:
        return self.metadata.get("action_taken")

    @property
    def outcome_held(self) -> bool | None:
        return parse_outcome(self.metadata.get("outcome_held") or self._tag("outcome"))

    @property
    def when(self) -> str:
        return (self.metadata.get("occurred_at") or self.occurred_at or "")[:10]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            technician=self.technician,
            unit_id=self.unit_id,
            error_code=self.error_code,
            outcome_held=self.outcome_held,
            action_category=self.action_category,
            action_taken=self.action_taken,
            record_type=self.record_type,
            when=self.when,
        )
        return data

    @classmethod
    def from_hindsight(cls, result: dict[str, Any]) -> "MemoryHit":
        scores = result.get("scores") or {}
        return cls(
            id=str(result.get("id", "")),
            text=result.get("text", ""),
            type=result.get("type") or "world",
            score=float(scores.get("final") or 0.0),
            source="hindsight",
            occurred_at=result.get("occurred_start") or result.get("mentioned_at"),
            document_id=result.get("document_id"),
            tags=list(result.get("tags") or []),
            metadata=dict(result.get("metadata") or {}),
        )

    @classmethod
    def from_record(cls, record: RepairRecord, score: float) -> "MemoryHit":
        return cls(
            id=record.record_id,
            text=record.content,
            type="world" if record.record_type == "repair" else "experience",
            score=round(score, 4),
            source="local_fallback",
            occurred_at=record.occurred_at,
            document_id=record.document_id,
            tags=record.tags,
            metadata=record.metadata,
        )


def _untag_tech(value: str | None) -> str | None:
    if not value:
        return None
    # "tech-dave" -> "Tech_Dave"
    return "_".join(part.capitalize() for part in value.split("-"))


# --------------------------------------------------------------------- summary
@dataclass
class FixStats:
    action: str
    category: str
    attempts: int = 0
    held: int = 0
    root_cause: str = ""
    first_by: str | None = None
    first_on: str | None = None
    first_unit: str | None = None
    technicians: list[str] = field(default_factory=list)
    sites: dict[str, int] = field(default_factory=dict)

    @property
    def hold_rate(self) -> float:
        return self.held / self.attempts if self.attempts else 0.0


@dataclass
class OutcomeSummary:
    sample_size: int
    manual: list[FixStats]
    field: list[FixStats]
    no_defect_checks: list[dict[str, Any]]
    unit_history: list[dict[str, Any]]


def _outcome_hits(hits: list[MemoryHit]) -> list[MemoryHit]:
    """One hit per retained document, only records with a known outcome."""
    seen: set[str] = set()
    unique = []
    for hit in hits:
        key = hit.document_id or hit.id
        if key in seen or hit.outcome_held is None or not hit.action_taken:
            continue
        seen.add(key)
        unique.append(hit)
    return sorted(unique, key=lambda h: h.when)


def summarize_outcomes(hits: list[MemoryHit], unit_id: str | None = None) -> OutcomeSummary:
    groups: dict[tuple[str, str], FixStats] = {}
    no_defect: list[dict[str, Any]] = []
    unit_history: list[dict[str, Any]] = []
    usable = _outcome_hits(hits)

    for hit in usable:
        action = hit.action_taken or ""
        category = hit.action_category or "unknown"
        entry = {"date": hit.when, "technician": hit.technician, "unit_id": hit.unit_id, "action": action, "held": hit.outcome_held}
        if unit_id and hit.unit_id == unit_id:
            unit_history.append(entry)
        if action.lower().startswith(NO_DEFECT_PREFIX):
            no_defect.append(entry)
            continue
        stats = groups.setdefault((category, action), FixStats(action=action, category=category))
        stats.attempts += 1
        if hit.outcome_held:
            stats.held += 1
            if stats.first_by is None:
                stats.first_by, stats.first_on, stats.first_unit = hit.technician, hit.when, hit.unit_id
            root = hit.metadata.get("root_cause", "")
            if root and not root.startswith("unconfirmed"):
                stats.root_cause = root
            site = hit.metadata.get("site")
            if site:
                stats.sites[site] = stats.sites.get(site, 0) + 1
        if hit.technician and hit.technician not in stats.technicians:
            stats.technicians.append(hit.technician)

    ordered = sorted(groups.values(), key=lambda s: (-s.held, -s.attempts))
    return OutcomeSummary(
        sample_size=len(usable),
        manual=[s for s in ordered if s.category == "manual"],
        field=[s for s in ordered if s.category == "field"],
        no_defect_checks=no_defect,
        unit_history=unit_history,
    )


def memory_delta(
    summary: OutcomeSummary, manual_action: str | None, source: str, site: str | None = None
) -> dict[str, Any]:
    """The headline comparison between what the OEM manual says and what held in the field.

    Field fixes that held at the technician's own site rank first: the same error code can have a
    different root cause in a different environment (e.g. coastal corrosion vs desert UV damage).
    """
    primary = next((s for s in summary.manual if manual_action and s.action.lower() == manual_action.lower()), None)
    field_fixes = sorted(summary.field, key=lambda s: (-(s.sites.get(site, 0) if site else 0), -s.held, -s.attempts))
    best_field = field_fixes[0] if field_fixes else None
    local = bool(site and best_field and best_field.sites.get(site))
    manual_attempts = primary.attempts if primary else 0
    manual_held = primary.held if primary else 0

    has_delta = bool(best_field and best_field.held and (manual_attempts - manual_held > 0 or best_field.held > manual_held))
    headline = None
    if has_delta and best_field:
        manual_part = (
            f"; the OEM step held {manual_held} of {manual_attempts} time{'s' if manual_attempts != 1 else ''} in recalled history"
            if manual_attempts
            else ""
        )
        where = f", including {best_field.sites[site]} at {site}" if local else ""
        headline = (
            f"OEM manual recommends “{manual_action}”, but {best_field.first_by or 'a technician'}'s field fix "
            f"({best_field.root_cause or best_field.action}) held on {best_field.held} of {best_field.attempts} "
            f"occurrence{'s' if best_field.attempts != 1 else ''} since {best_field.first_on}{where}{manual_part}."
        )
        if summary.no_defect_checks:
            cases = ", ".join(f"{c['unit_id']} {c['date']}" for c in summary.no_defect_checks[:3])
            headline += (
                f" Exception: {len(summary.no_defect_checks)} time(s) a known field pattern was checked and was not "
                f"the cause ({cases})."
            )

    return {
        "has_delta": has_delta,
        "headline": headline,
        "manual_action": manual_action,
        "manual_attempts": manual_attempts,
        "manual_held": manual_held,
        "field_fixes": [
            {
                "action": s.action,
                "root_cause": s.root_cause,
                "attempts": s.attempts,
                "held": s.held,
                "discovered_by": s.first_by,
                "discovered_on": s.first_on,
                "discovered_unit": s.first_unit,
                "technicians": s.technicians,
                "sites": s.sites,
            }
            for s in field_fixes[:3]
        ],
        "site": site,
        "site_specific": local,
        "no_defect_checks": summary.no_defect_checks,
        "unit_history": summary.unit_history,
        "sample_size": summary.sample_size,
        "source": source,
    }


def render_local_synthesis(summary: OutcomeSummary, model_name: str, error_code: str, title: str) -> str:
    """Deterministic stand-in for Hindsight reflect, built from the same outcome statistics."""
    if not summary.sample_size:
        return f"No recorded repair outcomes for {error_code} on {model_name} yet."
    lines = [f"**{error_code} ({title}) on {model_name}**: {summary.sample_size} recorded repair outcomes.", ""]
    for s in summary.manual[:3]:
        lines.append(f"- OEM step “{s.action}”: held {s.held}/{s.attempts}.")
    for s in summary.field[:3]:
        sites = ", ".join(f"{site} ({n})" for site, n in s.sites.items())
        lines.append(
            f"- Field fix “{s.action}”: held {s.held}/{s.attempts}; first confirmed by {s.first_by} on {s.first_on} "
            f"({s.first_unit}). Root cause: {s.root_cause or 'n/a'}." + (f" Held at: {sites}." if sites else "")
        )
    if summary.no_defect_checks:
        lines.append(
            f"- {len(summary.no_defect_checks)} time(s) the known field pattern was checked and was NOT the cause "
            f"({', '.join(c['unit_id'] or '?' for c in summary.no_defect_checks)}); keep the OEM path as the fallback."
        )
    best = summary.field[0] if summary.field else None
    if best and best.hold_rate >= 0.6:
        lines += ["", f"**Guidance:** check “{best.root_cause or best.action}” first; it is quick and resolved most cases."]
    return "\n".join(lines)
