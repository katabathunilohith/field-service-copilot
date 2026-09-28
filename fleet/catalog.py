"""Equipment catalog: models, OEM manual sections, units, sites and live telemetry,
plus entity extraction from a technician's free-text question."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

from config import DATA_DIR
from memory.bank_schemas import now_iso

_UNIT_RE = re.compile(r"\b(CHL|INV|ELV)[\s-]?(\d{4})\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\b([A-Z]{1,3})\s?-?\s?(\d{2,3})\b", re.IGNORECASE)
# Only an explicit "Tech_Name" counts; a bare name ("Dave's fix") refers to someone else.
_TECH_RE = re.compile(r"\btech[\s_,.-]+([a-z]{2,20})\b", re.IGNORECASE)


@dataclass
class ParsedQuery:
    model_key: str | None
    model_name: str | None
    fleet: str | None
    error_code: str | None
    error_title: str | None
    unit_id: str | None
    site: str | None
    technician_id: str | None

    @property
    def complete(self) -> bool:
        return bool(self.model_key and self.error_code)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Catalog:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.models: dict[str, Any] = data["models"]
        self.units: dict[str, Any] = data["units"]
        self.sites: dict[str, Any] = data["sites"]
        self.technicians: dict[str, Any] = {t["id"]: t for t in data["technicians"]}
        self._aliases = sorted(
            ((alias, key) for key, m in self.models.items() for alias in m["aliases"]),
            key=lambda pair: -len(pair[0]),
        )
        self._code_to_model = {code: key for key, m in self.models.items() for code in m["error_codes"]}

    # ------------------------------------------------------------------ lookups
    def site_name(self, unit_id: str) -> str:
        unit = self.units.get(unit_id)
        return self.sites[unit["site"]]["name"] if unit else "unknown site"

    def technician_role(self, technician_id: str) -> str:
        return self.technicians.get(technician_id, {}).get("role", "Technician")

    def error_spec(self, model_key: str, error_code: str) -> dict[str, Any] | None:
        return self.models.get(model_key, {}).get("error_codes", {}).get(error_code)

    def manual(self, model_key: str, error_code: str) -> dict[str, Any] | None:
        spec = self.error_spec(model_key, error_code)
        if not spec:
            return None
        model = self.models[model_key]
        return {
            "model": model["name"],
            "error_code": error_code,
            "title": spec["title"],
            "severity": spec["severity"],
            "document": model["manual_doc"],
            **spec["manual"],
        }

    def manual_index(self, model_key: str) -> list[dict[str, str]]:
        return [
            {"error_code": code, "title": spec["title"]}
            for code, spec in self.models.get(model_key, {}).get("error_codes", {}).items()
        ]

    def telemetry(self, unit_id: str, window_hours: int = 24) -> dict[str, Any] | None:
        unit = self.units.get(unit_id)
        if not unit:
            return None
        channels: dict[str, Any] = {}
        for name, reading in self.data["telemetry_baselines"][unit["model"]].items():
            channels[name] = {**reading, "status": "normal"}
        for name, reading in (unit.get("telemetry") or {}).items():
            channels[name] = {**reading, "status": "abnormal" if reading.get("flag") and "within spec" not in reading["flag"] else "normal"}
        return {
            "unit_id": unit_id,
            "model": self.models[unit["model"]]["name"],
            "site": self.sites[unit["site"]]["name"],
            "location": unit.get("location"),
            "installed": unit.get("installed"),
            "window_hours": window_hours,
            "captured_at": now_iso(),
            "channels": channels,
            "events": unit.get("events", []),
        }

    # ------------------------------------------------------------------ parsing
    def parse(self, text: str, *, unit_hint: str | None = None, technician_hint: str | None = None) -> ParsedQuery:
        # Model numbers look like unit IDs (INV-9000), so take the first match that is a real unit.
        unit_id = next(
            (uid for m in _UNIT_RE.finditer(text) if (uid := f"{m.group(1).upper()}-{m.group(2)}") in self.units),
            None,
        )
        if not unit_id and unit_hint in self.units:
            unit_id = unit_hint

        model_key = self.units[unit_id]["model"] if unit_id else None
        lowered = text.lower()
        if not model_key:
            model_key = next((key for alias, key in self._aliases if alias in lowered), None)

        error_code = None
        for prefix, digits in _CODE_RE.findall(text):
            code = f"{prefix.upper()}-{digits}"
            owner = self._code_to_model.get(code)
            if owner and (model_key is None or owner == model_key):
                error_code, model_key = code, owner
                break

        tech = None
        if m := _TECH_RE.search(text):
            candidate = f"Tech_{m.group(1).capitalize()}"
            tech = candidate if candidate in self.technicians else None
        if not tech and technician_hint in self.technicians:
            tech = technician_hint

        model = self.models.get(model_key) if model_key else None
        spec = self.error_spec(model_key, error_code) if model_key and error_code else None
        return ParsedQuery(
            model_key=model_key,
            model_name=model["name"] if model else None,
            fleet=model["fleet"] if model else None,
            error_code=error_code,
            error_title=spec["title"] if spec else None,
            unit_id=unit_id,
            site=self.site_name(unit_id) if unit_id else None,
            technician_id=tech,
        )

    # ------------------------------------------------------------------ public
    def public(self) -> dict[str, Any]:
        return {
            "fleet": self.data["fleet"],
            "technicians": list(self.technicians.values()),
            "models": [
                {
                    "key": key,
                    "name": m["name"],
                    "fleet": m["fleet"],
                    "category": m["category"],
                    "error_codes": [{"code": c, "title": s["title"], "severity": s["severity"]} for c, s in m["error_codes"].items()],
                }
                for key, m in self.models.items()
            ],
            "units": [
                {"id": uid, "model": u["model"], "model_name": self.models[u["model"]]["name"], "site": self.sites[u["site"]]["name"],
                 "location": u.get("location")}
                for uid, u in sorted(self.units.items())
            ],
            "sample_prompts": self.data.get("sample_prompts", []),
        }


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    return Catalog(json.loads((DATA_DIR / "equipment_catalog.json").read_text()))
