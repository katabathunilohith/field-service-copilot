"""System prompts, context assembly and the deterministic no-LLM brief."""

from __future__ import annotations

import json
from typing import Any

from memory.insights import MemoryHit

_SAFETY = (
    "- Safety first: include lockout/tagout (or hoistway entry procedures for elevators) where relevant. "
    "Never advise bypassing interlocks, safety circuits or protective devices.\n"
)

COPILOT_SYSTEM = (
    "You are Field Service Copilot, a diagnostic assistant for technicians repairing commercial HVAC chillers, "
    "solar inverters and elevator hoists. You combine the OEM manual with long-term fleet memory: what other "
    "technicians actually found and whether their fixes held.\n\n"
    "Rules:\n"
    + _SAFETY
    + "- Ground every claim in the OEM manual, telemetry you fetched, or recalled memory. Cite memory inline as "
    "(Tech_Name, YYYY-MM-DD, UNIT). Never invent memories, measurements, part numbers or hold rates.\n"
    "- When memory shows an OEM step failing and a field fix holding, recommend the quick field check FIRST and keep "
    "the OEM step as the fallback. State hold counts explicitly (e.g. 'held 7 of 8').\n"
    "- If memory records cases where the field pattern was checked and was NOT the cause, tell the technician which "
    "measurement distinguishes the cases.\n"
    "- Workflow: call fetch_unit_telemetry to confirm the pattern on this unit, call verify_fix_outcome for the fix "
    "you intend to recommend, then call log_repair_ticket once with your recommendation. Then answer.\n\n"
    "Answer in concise, field-ready markdown under 350 words with exactly these sections:\n"
    "### Likely root cause\n### Recommended procedure\n(numbered steps with specs)\n"
    "### Memory delta: field vs. manual\n(what memory changed compared with the OEM manual, with citations)\n"
    "### Verify before you leave"
)

BASELINE_SYSTEM = (
    "You are an OEM-manual diagnostic assistant for technicians repairing commercial HVAC chillers, solar inverters "
    "and elevator hoists. You have the OEM service manual and live telemetry only. You have no access to field "
    "history or other technicians' findings.\n\n"
    "Rules:\n"
    + _SAFETY
    + "- Follow the OEM manual procedure. Ground claims in the manual excerpt or telemetry you fetched; never invent "
    "part numbers or measurements.\n"
    "- Call fetch_unit_telemetry when a unit is identified, then answer.\n\n"
    "Answer in concise, field-ready markdown under 300 words with exactly these sections:\n"
    "### Likely root cause\n### Recommended procedure\n(numbered steps with specs)\n### Verify before you leave"
)


def _fmt_manual(manual: dict[str, Any] | None) -> str:
    if not manual:
        return "No manual section matched. Use lookup_service_manual once the model and error code are known."
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(manual["procedure"], 1))
    parts = ", ".join(f"{p['part']} (${p['cost_usd']:,})" for p in manual["parts"]) or "none"
    return (
        f"{manual['document']} {manual['section']}: {manual['error_code']} {manual['title']} (severity {manual['severity']})\n"
        f"Probable causes: {'; '.join(manual['probable_causes'])}\n"
        f"Procedure:\n{steps}\n"
        f"Primary OEM remedy: {manual['primary_action']}. Parts: {parts}. Est. labor {manual['est_labor_hours']} h."
    )


def _fmt_hit(i: int, hit: MemoryHit) -> str:
    outcome = {True: "HELD", False: "DID NOT HOLD", None: "n/a"}[hit.outcome_held]
    who = hit.technician or "unknown tech"
    meta = f"{hit.type}, score {hit.score:.2f}, {hit.when or 'undated'}, {who}, {hit.unit_id or '-'}, outcome {outcome}"
    return f"[M{i}] ({meta}) {hit.text[:700]}"


def build_context(
    *,
    technician: dict[str, Any],
    parsed: dict[str, Any],
    manual: dict[str, Any] | None,
    hits: list[MemoryHit] | None,
    delta: dict[str, Any] | None,
    reflection: str | None,
    memory_source: str | None,
) -> str:
    equipment = (
        f"Model: {parsed.get('model_name') or 'not identified'} | Unit: {parsed.get('unit_id') or 'not identified'} | "
        f"Site: {parsed.get('site') or '-'} | Error: {parsed.get('error_code') or 'not identified'} "
        f"{('(' + parsed['error_title'] + ')') if parsed.get('error_title') else ''}"
    )
    sections = [
        f"## Technician\n{technician.get('id')} ({technician.get('role', 'Technician')}, {technician.get('years_experience', '?')} yrs)",
        f"## Equipment\n{equipment}",
        f"## OEM manual excerpt\n{_fmt_manual(manual)}",
    ]
    if hits is not None:
        body = "\n".join(_fmt_hit(i, h) for i, h in enumerate(hits, 1)) or "No relevant memories recalled."
        sections.append(f"## Recalled fleet memory (source: {memory_source})\n{body}")
    if delta is not None:
        stats = {k: delta[k] for k in ("manual_action", "manual_attempts", "manual_held", "field_fixes", "no_defect_checks", "unit_history")}
        sections.append(f"## Outcome statistics computed from memory\n{json.dumps(stats, ensure_ascii=False)[:3000]}")
    if reflection:
        sections.append(f"## Reflection synthesis (patterns learned over time)\n{reflection[:2500]}")
    return "\n\n".join(sections)


def render_fallback_brief(
    *, mode: str, parsed: dict[str, Any], manual: dict[str, Any] | None, delta: dict[str, Any] | None, reason: str
) -> str:
    """Deterministic answer used when the LLM is unavailable, so technicians still get help."""
    lines = [f"> **LLM unavailable** ({reason}). This brief was assembled directly from the manual"
             + (" and fleet memory statistics." if mode == "copilot" else ".")]
    best = (delta or {}).get("field_fixes", [None])[0] if delta and delta.get("field_fixes") else None
    use_field = mode == "copilot" and delta and delta.get("has_delta") and best
    lines.append("\n### Likely root cause")
    if use_field:
        lines.append(f"{best['root_cause'] or best['action']} (held {best['held']} of {best['attempts']} in fleet memory).")
    elif manual:
        lines.append("; ".join(manual["probable_causes"]))
    else:
        lines.append("Model and error code not identified; include both (e.g. '30XA E-412').")
    lines.append("\n### Recommended procedure")
    step = 1
    if use_field:
        lines.append(f"{step}. Field check first: {best['action']} (first confirmed by {best['discovered_by']} on {best['discovered_on']}).")
        step += 1
    for s in (manual or {}).get("procedure", []):
        lines.append(f"{step}. {s}")
        step += 1
    if mode == "copilot":
        lines.append("\n### Memory delta: field vs. manual")
        lines.append((delta or {}).get("headline") or "No field history contradicts the manual for this fault.")
    lines.append("\n### Verify before you leave")
    lines.append("Clear the alarm, run the unit under load, and confirm the fault does not return before closing the ticket.")
    return "\n".join(lines)
