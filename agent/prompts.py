"""System prompts, token-lean context assembly and the deterministic no-LLM brief.

Context is deliberately compact: Groq's free tier allows ~8k tokens per minute per
model, and every tool round re-sends the whole conversation.
"""

from __future__ import annotations

import re
from typing import Any

from memory.insights import MemoryHit

_SAFETY = (
    "- Safety first: open the procedure with lockout/tagout (hoistway entry procedure for elevators). "
    "Never advise bypassing interlocks, safety circuits or protective devices.\n"
)

COPILOT_SYSTEM = (
    "You are Field Service Copilot, a diagnostic assistant for technicians repairing commercial HVAC chillers, "
    "solar inverters and elevator hoists. You combine the OEM manual, live telemetry and fleet memory: what other "
    "technicians found and whether their fixes held.\n"
    "Rules:\n"
    + _SAFETY
    + "- Ground every claim in the context below. Cite memory inline as (Tech_Name, YYYY-MM-DD, UNIT), copying the "
    "unit exactly. Never invent memories, measurements or hold rates; if a part number is not in the context, name "
    "the part generically.\n"
    "- When memory shows a field fix holding where the OEM step failed, put the quick field check first and keep the "
    "OEM step as the fallback. Quote hold counts (e.g. 'held 7 of 7') and cite who first confirmed the fix, for "
    "example (Tech_Dave, 2026-09-03, CHL-0417).\n"
    "- If memory lists cases where the field pattern was ruled out, say which measurement tells the cases apart.\n"
    "- Telemetry and memory statistics are already in the context, so answer directly. Tools are optional: use "
    "verify_fix_outcome only for a fix the statistics do not cover, and fetch_unit_telemetry only for another unit. "
    "A work order is filed automatically from your answer.\n"
    "Answer in concise markdown (under 300 words) using plain '### ' headings (never bold a heading), exactly:\n"
    "### Likely root cause\n### Recommended procedure\n### Memory delta: field vs. manual\n### Verify before you leave"
)

BASELINE_SYSTEM = (
    "You are an OEM-manual diagnostic assistant for technicians repairing commercial HVAC chillers, solar inverters "
    "and elevator hoists. You have the OEM service manual and live telemetry only, with no field history.\n"
    "Rules:\n"
    + _SAFETY
    + "- Follow the OEM manual procedure. Ground claims in the manual or telemetry; never invent part numbers or "
    "measurements. Telemetry is already in the context, so answer directly.\n"
    "Answer in concise markdown (under 250 words) using plain '### ' headings (never bold a heading), exactly:\n"
    "### Likely root cause\n### Recommended procedure\n### Verify before you leave"
)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_manual(manual: dict[str, Any] | None) -> str:
    if not manual:
        return "No manual section matched (model or error code not identified)."
    steps = " ".join(f"{i}) {_clip(s, 170)}" for i, s in enumerate(manual["procedure"], 1))
    parts = ", ".join(f"{p['part']} ${p['cost_usd']:,}" for p in manual["parts"]) or "none"
    return (
        f"{manual['document']} {manual['section']} · {manual['error_code']} {manual['title']} ({manual['severity']}). "
        f"Probable causes: {'; '.join(manual['probable_causes'])}. Procedure: {steps} "
        f"Primary OEM remedy: {manual['primary_action']} (parts: {parts}; ~{manual['est_labor_hours']} h)."
    )


def fmt_telemetry(telemetry: dict[str, Any] | None) -> str:
    if not telemetry:
        return "No unit identified, so no telemetry."
    abnormal, normal = [], []
    for name, r in telemetry["channels"].items():
        value = f"{name}={r['value']}{(' ' + r['unit']) if r.get('unit') else ''}"
        if r.get("status") == "abnormal":
            abnormal.append(f"{value} (normal {r.get('normal')}; {r.get('flag')})")
        else:
            normal.append(value)
    events = "; ".join(telemetry.get("events") or []) or "none"
    return (
        f"{telemetry['unit_id']} at {telemetry['site']} ({telemetry.get('location') or 'n/a'}), last {telemetry['window_hours']} h. "
        f"ABNORMAL: {'; '.join(abnormal) or 'none'}. Normal: {', '.join(normal)}. Events: {events}."
    )


def _fmt_hit(i: int, hit: MemoryHit) -> str:
    outcome = {True: "held", False: "did not hold", None: ""}[hit.outcome_held]
    meta = " · ".join(x for x in (hit.type, hit.when, hit.technician, hit.unit_id, outcome) if x)
    return f"[M{i}] ({meta}) {_clip(hit.text, 260)}"


def fmt_stats(delta: dict[str, Any] | None) -> str:
    if not delta or not delta.get("sample_size"):
        return "No recorded outcomes for this fault yet."
    lines = []
    if delta.get("manual_action"):
        lines.append(f"- OEM step '{delta['manual_action']}': held {delta['manual_held']}/{delta['manual_attempts']}.")
    for f in delta.get("field_fixes") or []:
        sites = ", ".join(f"{s} {n}" for s, n in (f.get("sites") or {}).items())
        lines.append(
            f"- Field fix '{f['action']}': held {f['held']}/{f['attempts']}; first confirmed by {f['discovered_by']} "
            f"on {f['discovered_on']} ({f['discovered_unit']}); root cause: {f['root_cause'] or 'n/a'}"
            + (f"; held at: {sites}" if sites else "") + "."
        )
    for c in delta.get("no_defect_checks") or []:
        lines.append(f"- Ruled out: known field pattern checked on {c['unit_id']} {c['date']} by {c['technician']} and was NOT the cause.")
    history = delta.get("unit_history") or []
    if history:
        lines.append("- This unit's history: " + "; ".join(
            f"{h['date']} {h['technician']}: {_clip(h['action'], 70)} ({'held' if h['held'] else 'did not hold'})" for h in history[-4:]
        ) + ".")
    lines.append(f"(stats from {delta['sample_size']} recorded outcomes; source: {delta['source']})")
    return "\n".join(lines)


def build_context(
    *,
    technician: dict[str, Any],
    parsed: dict[str, Any],
    manual: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
    hits: list[MemoryHit] | None,
    delta: dict[str, Any] | None,
    reflection: str | None,
    memory_source: str | None,
) -> str:
    equipment = (
        f"{parsed.get('model_name') or 'model not identified'} · unit {parsed.get('unit_id') or 'not identified'} · "
        f"site {parsed.get('site') or '-'} · error {parsed.get('error_code') or 'not identified'}"
        + (f" ({parsed['error_title']})" if parsed.get("error_title") else "")
    )
    sections = [
        f"TECHNICIAN: {technician.get('id')} ({technician.get('role', 'Technician')}, {technician.get('years_experience', '?')} yrs)",
        f"EQUIPMENT: {equipment}",
        f"OEM MANUAL: {fmt_manual(manual)}",
        f"TELEMETRY: {fmt_telemetry(telemetry)}",
    ]
    if hits is not None:
        body = "\n".join(_fmt_hit(i, h) for i, h in enumerate(hits, 1)) or "No relevant memories recalled."
        sections.append(f"FLEET MEMORY (source: {memory_source}):\n{body}")
    if delta is not None:
        sections.append(f"OUTCOME STATISTICS FROM MEMORY:\n{fmt_stats(delta)}")
    if reflection:
        sections.append(f"REFLECTION (patterns learned over time):\n{_clip(reflection, 900)}")
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
