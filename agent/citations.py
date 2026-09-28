"""Verify the memory citations in an answer against the repair ledger.

The Copilot cites field memory as (Tech_Name, YYYY-MM-DD, UNIT). Models occasionally
garble one element (a unit ID from a neighbouring record, a shifted date), so every
citation is checked against the ledger that mirrors what was retained to Hindsight.
Nothing is rewritten: mismatches are flagged with the ledger's version, so the
technician can see what was actually recorded.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable

from memory.bank_schemas import RepairRecord

# gpt-oss likes typographic hyphens (U+2010-U+2015, U+2212); normalise before matching.
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_TECH = r"(Tech_[A-Z][a-z]+)"
_DATE = r"(20\d\d-\d\d-\d\d)"
_UNIT = r"((?:CHL|INV|ELV)-\d{4})"
# Tech, date and (optionally) unit close together, in any common separator style.
_CITATION = re.compile(rf"{_TECH}[^\n()|]{{0,24}}?{_DATE}(?:[^\n()|]{{0,24}}?{_UNIT})?")


def normalize_text(text: str) -> str:
    return text.translate(_DASHES)


def normalize_markdown(answer: str) -> str:
    """Tidy model output: plain hyphens, and headings the model wrapped in bold (**### X**)."""
    text = normalize_text(answer)
    return re.sub(r"^\s*\*\*\s*(#{1,4}\s[^*\n]+?)\s*\*\*\s*$", r"\1", text, flags=re.MULTILINE)


@dataclass
class Citation:
    text: str
    technician: str
    date: str
    unit: str | None
    status: str  # verified | unit_mismatch | not_found
    ledger_units: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def verify_citations(answer: str, records: Iterable[RepairRecord]) -> dict:
    by_tech_date: dict[tuple[str, str], set[str]] = {}
    for r in records:
        by_tech_date.setdefault((r.technician_id, r.occurred_at[:10]), set()).add(r.unit_id)

    seen: set[tuple[str, str, str | None]] = set()
    citations: list[Citation] = []
    for m in _CITATION.finditer(normalize_text(answer)):
        tech, date, unit = m.group(1), m.group(2), m.group(3)
        key = (tech, date, unit)
        if key in seen:
            continue
        seen.add(key)
        units = sorted(by_tech_date.get((tech, date), set()))
        if not units:
            status = "not_found"
        elif unit and unit not in units:
            status = "unit_mismatch"
        else:
            status = "verified"
        citations.append(Citation(m.group(0).strip(), tech, date, unit, status, units))

    verified = sum(c.status == "verified" for c in citations)
    return {
        "total": len(citations),
        "verified": verified,
        "issues": [c.to_dict() for c in citations if c.status != "verified"],
        "citations": [c.to_dict() for c in citations],
    }
