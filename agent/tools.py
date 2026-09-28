"""Tools the diagnostic agent can call, with schemas, argument validation and executors.

Baseline mode only gets the OEM manual and telemetry. Copilot mode also gets
memory-backed outcome verification and ticket logging.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.groq_client import ToolCall
from fleet.catalog import Catalog
from fleet.tickets import TicketLedger
from memory.bank_schemas import resolve_bank_id
from memory.hindsight_wrapper import HindsightMemory
from memory.insights import summarize_outcomes

_MODEL_KEYS = ["carrier-30xa", "apex-inv9000", "kone-mx20"]

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "lookup_service_manual": {
        "type": "function",
        "function": {
            "name": "lookup_service_manual",
            "description": "Return the OEM service-manual section (probable causes, procedure, parts, labor) for a model and error code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "enum": _MODEL_KEYS, "description": "Equipment model key"},
                    "error_code": {"type": "string", "description": "Error code, e.g. E-412, GF-17, F-082"},
                },
                "required": ["model", "error_code"],
            },
        },
    },
    "fetch_unit_telemetry": {
        "type": "function",
        "function": {
            "name": "fetch_unit_telemetry",
            "description": "Fetch live telemetry channels (with normal ranges and anomaly flags) and recent controller events for one unit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit_id": {"type": "string", "description": "Unit ID such as CHL-0417, INV-3140, ELV-0215"},
                    "window_hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
                },
                "required": ["unit_id"],
            },
        },
    },
    "verify_fix_outcome": {
        "type": "function",
        "function": {
            "name": "verify_fix_outcome",
            "description": (
                "Check fleet memory for how often a proposed fix actually held for this model and error code, "
                "who applied it, and any cases where it was not the root cause. Call before recommending a fix."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "enum": _MODEL_KEYS},
                    "error_code": {"type": "string"},
                    "proposed_action": {"type": "string", "description": "The fix you are considering, in plain words"},
                    "unit_id": {"type": "string", "description": "Optional unit to include its repair history"},
                },
                "required": ["model", "error_code", "proposed_action"],
            },
        },
    },
    "log_repair_ticket": {
        "type": "function",
        "function": {
            "name": "log_repair_ticket",
            "description": "Open a repair ticket with your diagnosis and recommended action so the outcome can be confirmed later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit_id": {"type": "string"},
                    "error_code": {"type": "string"},
                    "diagnosis": {"type": "string", "description": "Suspected root cause"},
                    "recommended_action": {"type": "string", "description": "The first fix the technician should perform"},
                    "action_category": {"type": "string", "enum": ["manual", "field"], "description": "OEM manual step or field-discovered fix"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
                    "parts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["unit_id", "error_code", "diagnosis", "recommended_action", "action_category"],
            },
        },
    },
}

BASELINE_TOOLS = ["lookup_service_manual", "fetch_unit_telemetry"]
COPILOT_TOOLS = ["lookup_service_manual", "fetch_unit_telemetry", "verify_fix_outcome", "log_repair_ticket"]

_CODE = re.compile(r"^\s*([A-Za-z]{1,3})\s*-?\s*(\d{2,3})\s*$")
_UNIT = re.compile(r"^\s*(CHL|INV|ELV)\s*-?\s*(\d{4})\s*$", re.IGNORECASE)


def tool_specs(names: list[str]) -> list[dict[str, Any]]:
    return [TOOL_SPECS[n] for n in names]


def normalize_code(value: Any) -> str:
    m = _CODE.match(str(value or ""))
    return f"{m.group(1).upper()}-{m.group(2)}" if m else str(value or "").strip().upper()


def normalize_unit(value: Any) -> str:
    m = _UNIT.match(str(value or ""))
    return f"{m.group(1).upper()}-{m.group(2)}" if m else str(value or "").strip().upper()


_STOPWORDS = {"the", "and", "with", "for", "then", "from", "into", "per", "any", "all", "its", "fit", "new", "check"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / max(1, len(ta | tb))


def canonical_action(text: str, candidates: list[str], threshold: float = 0.3) -> str | None:
    """Map free-text LLM wording onto a known fix so outcome statistics accumulate on one entry."""
    best = max(candidates, key=lambda c: similarity(text, c), default=None)
    return best if best and similarity(text, best) >= threshold else None


@dataclass
class ToolContext:
    run_id: str
    technician_id: str
    catalog: Catalog
    memory: HindsightMemory | None
    tickets: TicketLedger | None
    allowed: list[str]
    ticket: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


class ToolExecutor:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "lookup_service_manual": self._lookup_service_manual,
            "fetch_unit_telemetry": self._fetch_unit_telemetry,
            "verify_fix_outcome": self._verify_fix_outcome,
            "log_repair_ticket": self._log_repair_ticket,
        }

    async def execute(self, call: ToolCall) -> dict[str, Any]:
        start = time.perf_counter()
        if "__invalid__" in call.arguments:
            result = {"error": f"Arguments were not valid JSON ({call.arguments['__invalid__']}). Retry with a JSON object."}
        elif call.name not in self.ctx.allowed:
            result = {"error": f"Unknown or unavailable tool '{call.name}'. Available: {', '.join(self.ctx.allowed)}"}
        else:
            missing = [
                p for p in TOOL_SPECS[call.name]["function"]["parameters"].get("required", [])
                if call.arguments.get(p) in (None, "")
            ]
            if missing:
                result = {"error": f"Missing required argument(s): {', '.join(missing)}"}
            else:
                try:
                    result = await self._handlers[call.name](call.arguments)
                except Exception as exc:  # a tool bug must not kill the diagnostic loop
                    result = {"error": f"{call.name} failed: {type(exc).__name__}: {exc}"}
        self.ctx.trace.append({
            "id": call.id,
            "name": call.name,
            "arguments": {k: v for k, v in call.arguments.items() if k != "__invalid__"},
            "raw_arguments": call.raw_arguments,
            "repaired": call.repaired,
            "repair_notes": call.repair_notes,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "error": result.get("error"),
            "result": result,
        })
        return result

    async def execute_all(self, calls: list[ToolCall]) -> list[tuple[ToolCall, dict[str, Any]]]:
        results = await asyncio.gather(*(self.execute(c) for c in calls))
        return list(zip(calls, results))

    # ------------------------------------------------------------------ handlers
    async def _lookup_service_manual(self, args: dict[str, Any]) -> dict[str, Any]:
        model, code = str(args["model"]).strip().lower(), normalize_code(args["error_code"])
        manual = self.ctx.catalog.manual(model, code)
        if manual is None:
            return {"error": f"No manual section for {code} on {model}.", "known_codes": self.ctx.catalog.manual_index(model)}
        return manual

    async def _fetch_unit_telemetry(self, args: dict[str, Any]) -> dict[str, Any]:
        unit = normalize_unit(args["unit_id"])
        try:
            hours = int(args.get("window_hours") or 24)
        except (TypeError, ValueError):
            hours = 24
        data = self.ctx.catalog.telemetry(unit, max(1, min(hours, 168)))
        return data if data else {"error": f"Unknown unit '{unit}'."}

    async def _verify_fix_outcome(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.ctx.memory is None:
            return {"error": "Fleet memory is disabled in this mode."}
        model, code = str(args["model"]).strip().lower(), normalize_code(args["error_code"])
        unit = normalize_unit(args["unit_id"]) if args.get("unit_id") else None
        proposed = str(args["proposed_action"])
        fleet = self.ctx.catalog.models.get(model, {}).get("fleet")
        hits, recall, stats_source = await self.ctx.memory.fix_outcome_hits(
            resolve_bank_id(fleet), model, code, query_hint=proposed, run_id=self.ctx.run_id
        )
        summary = summarize_outcomes(hits, unit)
        groups = summary.manual + summary.field
        best = max(groups, key=lambda s: similarity(proposed, s.action), default=None)
        match = best if best and similarity(proposed, best.action) >= 0.15 else None

        def view(s: Any) -> dict[str, Any]:
            return {
                "action": s.action, "category": s.category, "attempts": s.attempts, "held": s.held,
                "hold_rate": round(s.hold_rate, 2), "root_cause": s.root_cause or None,
                "first_confirmed_by": s.first_by, "first_confirmed_on": s.first_on, "first_unit": s.first_unit,
                "technicians": s.technicians, "sites": s.sites,
            }

        return {
            "memory_source": recall.source,
            "stats_source": stats_source,
            "records_considered": summary.sample_size,
            "proposed_action": proposed,
            "matched_history": view(match) if match else None,
            "all_fixes": [view(s) for s in groups[:6]],
            "field_pattern_checked_but_not_cause": summary.no_defect_checks,
            "unit_history": summary.unit_history,
        }

    async def _log_repair_ticket(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.ctx.tickets is None:
            return {"error": "Ticketing is disabled in this mode."}
        if self.ctx.ticket:
            return {"ticket_id": self.ctx.ticket["id"], "status": "already_logged"}
        unit = normalize_unit(args["unit_id"])
        unit_info = self.ctx.catalog.units.get(unit)
        if unit_info is None:
            return {"error": f"Unknown unit '{unit}'."}
        category = args.get("action_category") if args.get("action_category") in ("manual", "field") else "manual"
        priority = args.get("priority") if args.get("priority") in ("low", "normal", "high", "critical") else "normal"
        parts = args.get("parts") or []
        if isinstance(parts, str):
            parts = [p.strip() for p in re.split(r"[,;]", parts) if p.strip()]
        code = normalize_code(args["error_code"])
        recommended = str(args["recommended_action"])[:400]
        candidates = self.ctx.memory.fallback.known_actions(unit_info["model"], code) if self.ctx.memory else []
        manual = self.ctx.catalog.manual(unit_info["model"], code)
        if manual:
            candidates.append(manual["primary_action"])
        ticket = self.ctx.tickets.create(
            unit_id=unit,
            model=unit_info["model"],
            error_code=code,
            technician_id=self.ctx.technician_id,
            diagnosis=str(args["diagnosis"])[:600],
            recommended_action=recommended,
            canonical_action=canonical_action(recommended, candidates),
            action_category=category,
            priority=priority,
            parts=[str(p)[:120] for p in parts][:10],
            run_id=self.ctx.run_id,
            created_via="agent_tool",
        )
        self.ctx.ticket = ticket
        return {"ticket_id": ticket["id"], "status": ticket["status"]}


def tool_result_message(call: ToolCall, result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)[:6000]}
