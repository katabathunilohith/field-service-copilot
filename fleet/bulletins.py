"""Field service bulletins: turn repeated field evidence into a publishable document.

When memory shows a field fix beating the OEM procedure, a service manager can draft
a Technical Service Bulletin. Hindsight's structured reflect writes the narrative
(under the bank's safety directives); the evidence numbers are computed from the
repair ledger so the LLM cannot inflate them. Approving a bulletin retains it to
memory and installs a Hindsight directive scoped to that model and error code, so
every future reflection on that fault follows the approved guidance.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from fleet.catalog import Catalog
from memory.bank_schemas import error_tag, model_tag, now_iso, resolve_bank_id
from memory.hindsight_wrapper import HindsightError, HindsightMemory
from memory.insights import summarize_outcomes

_SAFETY_STEP = re.compile(r"lockout|tagout|\bloto\b|isolat|de-?energi|zero.energy|hoistway entry", re.IGNORECASE)

BULLETIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short bulletin title naming the model, error code and fix"},
        "symptom": {"type": "string", "description": "What the technician observes on site"},
        "root_cause": {"type": "string", "description": "The field-verified root cause"},
        "recommended_procedure": {
            "type": "array", "items": {"type": "string"},
            "description": "Ordered steps, starting with the safety step and ending with verification",
        },
        "when_to_use_oem_procedure": {"type": "string", "description": "When to fall back to the OEM manual procedure"},
        "site_conditions": {"type": "string", "description": "Site or environmental factors that change the root cause, or 'none observed'"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["title", "symptom", "root_cause", "recommended_procedure", "when_to_use_oem_procedure", "confidence"],
}


class BulletinService:
    def __init__(self, path: Path, catalog: Catalog, memory: HindsightMemory) -> None:
        self._path = path
        self._catalog = catalog
        self._memory = memory
        self._lock = threading.Lock()
        self._bulletins: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    b = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._bulletins[b["id"]] = b

    # ------------------------------------------------------------- persistence
    def _save(self, bulletin: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._bulletins[bulletin["id"]] = bulletin
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as fh:
                fh.write(json.dumps(bulletin) + "\n")
        return bulletin

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._bulletins.values(), key=lambda b: b["created_at"], reverse=True)

    def get(self, bulletin_id: str) -> dict[str, Any] | None:
        return self._bulletins.get(bulletin_id)

    def _next_id(self) -> str:
        year = now_iso()[:4]
        return f"FSB-{year}-{len(self._bulletins) + 1:03d}"

    # ---------------------------------------------------------------- evidence
    def evidence(self, model_key: str, error_code: str) -> dict[str, Any]:
        """Hold-rate evidence computed from the repair ledger (never from LLM text)."""
        hits = self._memory.fallback.outcome_hits(model_key, error_code)
        summary = summarize_outcomes(hits)
        manual = self._catalog.manual(model_key, error_code) or {}
        primary = next((s for s in summary.manual if s.action == manual.get("primary_action")), None)
        dates = sorted(h.when for h in hits if h.when)
        return {
            "records": summary.sample_size,
            "period": [dates[0], dates[-1]] if dates else None,
            "oem_step": {
                "action": manual.get("primary_action"),
                "attempts": primary.attempts if primary else 0,
                "held": primary.held if primary else 0,
            },
            "field_fixes": [
                {
                    "action": s.action, "root_cause": s.root_cause, "attempts": s.attempts, "held": s.held,
                    "first_confirmed_by": s.first_by, "first_confirmed_on": s.first_on, "first_unit": s.first_unit,
                    "technicians": s.technicians, "sites": s.sites,
                }
                for s in summary.field[:2]
            ],
            "ruled_out": summary.no_defect_checks,
        }

    def candidates(self) -> list[dict[str, Any]]:
        """Faults where field evidence beats the manual strongly enough to justify a bulletin."""
        by_fault = {(b["model_key"], b["error_code"]): b for b in reversed(self.list())}
        out = []
        for model_key, model in self._catalog.models.items():
            for code, spec in model["error_codes"].items():
                ev = self.evidence(model_key, code)
                best = ev["field_fixes"][0] if ev["field_fixes"] else None
                if not best or best["held"] < 3 or best["held"] <= ev["oem_step"]["held"]:
                    continue
                existing = by_fault.get((model_key, code))
                out.append({
                    "model_key": model_key, "model_name": model["name"], "error_code": code, "error_title": spec["title"],
                    "evidence": ev,
                    "bulletin": {"id": existing["id"], "status": existing["status"]} if existing else None,
                })
        return sorted(out, key=lambda c: -c["evidence"]["field_fixes"][0]["held"])

    # ----------------------------------------------------------------- drafting
    def _template(self, model_key: str, error_code: str, ev: dict[str, Any]) -> dict[str, Any]:
        """Deterministic draft used when Hindsight reflect is unavailable."""
        spec = self._catalog.error_spec(model_key, error_code) or {}
        model = self._catalog.models[model_key]
        best = ev["field_fixes"][0]
        ratio = best["held"] / max(1, best["attempts"])
        sites = ", ".join(best["sites"]) or "none recorded"
        return {
            "title": f"{model['name']} {error_code}: {best['action'].split(',')[0]} before {ev['oem_step']['action']}",
            "symptom": spec.get("title", error_code),
            "root_cause": best["root_cause"] or best["action"],
            "recommended_procedure": [
                "Apply lockout/tagout (hoistway entry procedure for elevators) and verify zero energy.",
                best["action"] + ".",
                "Clear the alarm, restart, and run the unit under load to confirm the fault does not return.",
                f"If the fault persists, follow the OEM procedure: {ev['oem_step']['action']}.",
            ],
            "when_to_use_oem_procedure": (
                "When the field check finds no defect"
                + (f" (as on {', '.join(c['unit_id'] for c in ev['ruled_out'])})" if ev["ruled_out"] else "")
                + ", continue with the OEM procedure."
            ),
            "site_conditions": f"Field fix confirmed at: {sites}.",
            "confidence": "high" if ratio >= 0.85 and best["attempts"] >= 5 else "medium",
        }

    async def draft(self, model_key: str, error_code: str) -> dict[str, Any]:
        spec = self._catalog.error_spec(model_key, error_code)
        if spec is None:
            raise KeyError(f"{model_key}/{error_code}")
        model = self._catalog.models[model_key]
        ev = self.evidence(model_key, error_code)
        if not ev["field_fixes"]:
            raise ValueError("No field evidence for this fault yet")
        bank_id = resolve_bank_id(model["fleet"])
        generator, directives, memories, error = "hindsight_reflect", [], 0, None
        try:
            result = await self._memory.reflect_structured(
                bank_id,
                f"Draft a technical service bulletin for {model['name']} error {error_code} ({spec['title']}) based on "
                "field evidence: the verified root cause, the recommended procedure, when to fall back to the OEM "
                "procedure, and any site conditions that change the root cause.",
                [model_tag(model_key), error_tag(error_code)],
                BULLETIN_SCHEMA,
                timeout_s=60.0,
            )
            content, directives, memories = result["structured"], result["directives"], result["memories"]
        except (TimeoutError, HindsightError) as exc:
            content, generator, error = self._template(model_key, error_code, ev), "template", str(exc)[:200]
        if not self._memory.enabled:
            generator, error = "template", "Hindsight not configured"
        bulletin = {
            "id": self._next_id(),
            "status": "draft",
            "created_at": now_iso(),
            "model_key": model_key,
            "model_name": model["name"],
            "error_code": error_code,
            "error_title": spec["title"],
            "content": content,
            "evidence": ev,
            "generator": generator,
            "generator_error": error,
            "directives_applied": directives,
            "memories_consulted": memories,
        }
        return self._save(bulletin)

    # ---------------------------------------------------------------- approval
    async def approve(self, bulletin_id: str, approver: str) -> dict[str, Any]:
        bulletin = self.get(bulletin_id)
        if bulletin is None:
            raise KeyError(bulletin_id)
        model = self._catalog.models[bulletin["model_key"]]
        bank_id = resolve_bank_id(model["fleet"])
        publish: dict[str, Any] = {"memory": "skipped", "directive": "skipped"}
        if self._memory.enabled:
            item = {
                "content": f"[Technical service bulletin {bulletin['id']}, approved by {approver}] " + to_markdown(bulletin),
                "timestamp": now_iso(),
                "context": "technical service bulletin",
                "document_id": bulletin["id"].lower(),
                "tags": [model_tag(bulletin["model_key"]), error_tag(bulletin["error_code"]), "record:bulletin",
                         f"fleet:{model['fleet']}"],
                "metadata": {"record_type": "bulletin", "bulletin_id": bulletin["id"], "model": bulletin["model_key"],
                             "error_code": bulletin["error_code"], "status": "approved"},
            }
            try:
                raw = await self._memory.retain_item(bank_id, item, summary=f"bulletin {bulletin['id']} retained")
                publish["memory"] = f"queued ({raw.get('operation_id', 'ok')})"
            except (TimeoutError, HindsightError) as exc:
                publish["memory"] = f"failed: {str(exc)[:120]}"
            content = bulletin["content"]
            steps = content.get("recommended_procedure") or []
            first_fix = next((s for s in steps if not _SAFETY_STEP.search(s)), steps[0] if steps else "")
            directive = {
                "name": f"bulletin-{bulletin['id'].lower()}",
                "content": (f"Approved bulletin {bulletin['id']} for {bulletin['model_name']} {bulletin['error_code']}. "
                            f"Root cause: {content.get('root_cause', '')} First corrective action: {first_fix} "
                            f"Fall back to the OEM procedure when: {content.get('when_to_use_oem_procedure', '')}"),
                "priority": 60,
                "is_active": True,
                # Scoped: only reflections on this model and error code follow it.
                "tags": [model_tag(bulletin["model_key"]), error_tag(bulletin["error_code"])],
            }
            try:
                publish["directive"] = await self._memory.upsert_directive(bank_id, directive)
            except (TimeoutError, HindsightError) as exc:
                publish["directive"] = f"failed: {str(exc)[:120]}"
            self._memory.invalidate_reflection(bank_id, bulletin["model_key"], bulletin["error_code"])
        updated = {**bulletin, "status": "approved", "approved_by": approver, "approved_at": now_iso(), "publish": publish}
        return self._save(updated)


def to_markdown(b: dict[str, Any]) -> str:
    c, ev = b["content"], b["evidence"]
    best = ev["field_fixes"][0] if ev["field_fixes"] else {}
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(c.get("recommended_procedure") or [], 1))
    fixes = "\n".join(
        f"| {f['action']} | {f['held']}/{f['attempts']} | {f['first_confirmed_by']} ({f['first_confirmed_on']}, {f['first_unit']}) |"
        for f in ev["field_fixes"]
    )
    ruled = ", ".join(f"{r['unit_id']} ({r['date']})" for r in ev["ruled_out"]) or "none"
    return (
        f"# {b['id']}: {c.get('title', '')}\n\n"
        f"**Applies to:** {b['model_name']}, error {b['error_code']} ({b['error_title']})  \n"
        f"**Status:** {b['status']}{' by ' + b['approved_by'] if b.get('approved_by') else ''} · **Confidence:** {c.get('confidence', 'n/a')}  \n"
        f"**Evidence period:** {' to '.join(ev['period']) if ev.get('period') else 'n/a'} · {ev['records']} recorded outcomes\n\n"
        f"## Symptom\n{c.get('symptom', '')}\n\n"
        f"## Root cause\n{c.get('root_cause', '')}\n\n"
        f"## Recommended procedure\n{steps}\n\n"
        f"## When to use the OEM procedure\n{c.get('when_to_use_oem_procedure', '')}\n\n"
        f"## Site conditions\n{c.get('site_conditions') or 'none observed'}\n\n"
        f"## Field evidence\n| Fix | Held | First confirmed |\n|---|---|---|\n"
        f"| OEM: {ev['oem_step']['action']} | {ev['oem_step']['held']}/{ev['oem_step']['attempts']} | manual |\n{fixes}\n\n"
        f"Field pattern ruled out on: {ruled}. First confirmed by {best.get('first_confirmed_by', 'n/a')}.\n"
    )
