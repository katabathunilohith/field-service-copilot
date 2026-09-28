"""Diagnostic loop: Input -> Recall -> (Reflect) -> LLM tool loop -> Retain.

Two agents share the same LLM and tools so the comparison is fair:
* baseline: OEM manual + telemetry only, no memory, nothing retained
* copilot:  adds Hindsight recall/reflect, outcome verification, ticketing and post-run retain

Every stage can be streamed to the UI through an optional `emit(event, data)` callback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.citations import normalize_markdown, verify_citations
from agent.groq_client import GroqClient, LLMUnavailable, ToolCall
from agent.prompts import BASELINE_SYSTEM, COPILOT_SYSTEM, build_context, render_fallback_brief
from agent.tools import (
    BASELINE_TOOLS, COPILOT_TOOLS, TICKET_TOOL, ToolContext, ToolExecutor, tool_result_message, tool_specs,
)
from fleet.catalog import Catalog, ParsedQuery
from fleet.tickets import TicketLedger
from memory.bank_schemas import RepairRecord, now_iso, resolve_bank_id, unit_tag
from memory.field_notes import FieldNotes, format_briefing
from memory.hindsight_wrapper import HindsightMemory, RecallOutcome
from memory.insights import MemoryHit, OutcomeSummary, memory_delta, summarize_outcomes

log = logging.getLogger("copilot.agent")

Mode = Literal["baseline", "copilot"]
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
MAX_TOOL_ROUNDS = 4
MAX_HISTORY_TURNS = 6
REFLECT_WAIT_S = 0.3  # a diagnosis never waits on a cold reflect; it runs in the background instead
# How long a run waits for Hindsight to acknowledge the retain. The answer is already on screen
# by then; past this the write finishes in the background and the outbox guarantees delivery.
RETAIN_WAIT_S = 20.0


@dataclass
class DiagnoseRequest:
    query: str
    technician_id: str = "Tech_Alex"
    unit_id: str | None = None
    # Conversation so far, per pane: {"baseline": [...], "copilot": [...]}
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Read-only run (evaluation): no ticket is filed and nothing is retained to memory.
    dry_run: bool = False


def _new_run_id(mode: str) -> str:
    return f"{mode[:4]}-{secrets.token_hex(4)}"


def _clean_history(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns = [
        {"role": h["role"], "content": str(h.get("content", ""))[:1500]}
        for h in history or []
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
    ]
    return turns[-MAX_HISTORY_TURNS:]


def select_prompt_hits(fleet: list[MemoryHit], unit: list[MemoryHit], *, observations: int = 3,
                       facts: int = 3, unit_facts: int = 2) -> list[MemoryHit]:
    """Pick a small, diverse set for the prompt: consolidated observations first, then the
    strongest individual facts, then this unit's own history. De-duplicated by source."""
    chosen: list[MemoryHit] = []
    seen_docs: set[str] = set()
    seen_text: set[str] = set()

    def signature(hit: MemoryHit) -> str:
        return re.sub(r"[^a-z0-9]+", " ", hit.text.lower())[:110]

    def take(candidates: list[MemoryHit], cap: int) -> None:
        taken = 0
        for hit in candidates:
            if taken >= cap:
                return
            doc, sig = hit.document_id or hit.id, signature(hit)
            if doc in seen_docs or sig in seen_text or "record:note" in hit.tags:
                continue  # field notes have their own briefing block in the prompt
            seen_docs.add(doc)
            seen_text.add(sig)
            chosen.append(hit)
            taken += 1

    by_score = sorted(fleet, key=lambda h: -h.score)
    take([h for h in by_score if h.type == "observation"], observations)  # consolidated knowledge first
    take([h for h in by_score if h.type != "observation"], facts)
    take(sorted(unit, key=lambda h: -h.score), unit_facts)
    return chosen


def should_reflect(summary: OutcomeSummary, manual_held: int) -> tuple[bool, str]:
    """Reflect only when memory contradicts the manual; it is the expensive call."""
    manual_failures = sum(s.attempts - s.held for s in summary.manual)
    best_field = summary.field[0] if summary.field else None
    if manual_failures >= 2:
        return True, f"OEM steps failed {manual_failures}× in memory"
    if best_field and best_field.held >= 3 and best_field.held > manual_held:
        return True, "field fix outperforms the manual"
    return False, ""


async def _safe_emit(emit: Emit | None, event: str, data: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        await emit(event, data)
    except Exception:  # a disconnected client must never break the diagnosis
        log.debug("emit %s failed", event, exc_info=True)


class DiagnosticOrchestrator:
    def __init__(
        self, *, llm: GroqClient, memory: HindsightMemory, catalog: Catalog, tickets: TicketLedger,
        notes: FieldNotes | None = None,
    ) -> None:
        self.llm = llm
        self.memory = memory
        self.catalog = catalog
        self.tickets = tickets
        self.notes = notes or FieldNotes(memory, catalog)

    # ================================================================ public API
    async def diagnose(self, req: DiagnoseRequest, mode: str) -> dict[str, Any]:
        if mode == "compare":
            baseline, copilot = await asyncio.gather(self.run(req, "baseline"), self.run(req, "copilot"))
            return {"mode": "compare", "baseline": baseline, "copilot": copilot}
        if mode not in ("baseline", "copilot"):
            raise ValueError(f"unknown mode '{mode}'")
        return {"mode": mode, mode: await self.run(req, mode)}  # type: ignore[arg-type]

    async def run(self, req: DiagnoseRequest, mode: Mode, emit: Emit | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        run_id = _new_run_id(mode)
        parsed = self.catalog.parse(req.query, unit_hint=req.unit_id, technician_hint=req.technician_id)
        technician_id = parsed.technician_id or req.technician_id
        technician = self.catalog.technicians.get(technician_id, {"id": technician_id, "role": "Technician"})
        manual = self.catalog.manual(parsed.model_key, parsed.error_code) if parsed.complete else None
        warnings: list[str] = []
        if not parsed.complete:
            warnings.append("Could not identify both the equipment model and a known error code from the question.")
        await _safe_emit(emit, "parsed", {"run_id": run_id, "mode": mode, "parsed": parsed.to_dict(), "technician": technician})

        allowed = list(COPILOT_TOOLS if mode == "copilot" else BASELINE_TOOLS)
        if manual is not None:
            allowed.remove("lookup_service_manual")  # the manual section is already in the context
        tool_ctx = ToolContext(
            run_id=run_id, technician_id=technician_id, catalog=self.catalog,
            memory=self.memory if mode == "copilot" else None,
            tickets=self.tickets if mode == "copilot" else None,
            allowed=allowed,
        )
        executor = ToolExecutor(tool_ctx)

        # Telemetry is needed on every run, so fetch it up front instead of spending an LLM round on it.
        telemetry = None
        if parsed.unit_id:
            args = {"unit_id": parsed.unit_id, "window_hours": 24}
            telemetry = await executor.execute(
                ToolCall("prefetch-telemetry", "fetch_unit_telemetry", args, json.dumps(args)), prefetched=True
            )
            await _safe_emit(emit, "tool", {"phase": "done", **tool_ctx.trace[-1], "result": None})

        memory_view: dict[str, Any] | None = None
        hits: list[MemoryHit] | None = None
        delta: dict[str, Any] | None = None
        reflection_text: str | None = None
        timings: dict[str, int] = {}
        if mode == "copilot":
            memory_view, hits, delta, reflection_text, timings = await self._gather_memory(req.query, parsed, run_id, emit)

        context = build_context(
            technician=technician, parsed=parsed.to_dict(), manual=manual,
            telemetry=telemetry if telemetry and "error" not in telemetry else None,
            hits=hits, delta=delta, reflection=reflection_text, memory_source=(memory_view or {}).get("source"),
            notes=format_briefing((memory_view or {}).get("briefing")),
        )

        llm_started = time.perf_counter()
        model = self.llm.model if mode == "copilot" else self.llm.baseline_model
        llm_info: dict[str, Any] = {"model": model, "rounds": 0, "usage": {}, "recoveries": [], "waits": []}
        try:
            answer = await self._tool_loop(mode, model, context, req, executor, llm_info, emit)
        except LLMUnavailable as exc:
            warnings.append(f"LLM unavailable: {exc}")
            llm_info["error"] = str(exc)
            answer = render_fallback_brief(mode=mode, parsed=parsed.to_dict(), manual=manual, delta=delta, reason=str(exc))
        timings["llm_ms"] = int((time.perf_counter() - llm_started) * 1000)
        answer = normalize_markdown(answer)
        citations = verify_citations(answer, self.memory.fallback.all_records()) if mode == "copilot" else None
        await _safe_emit(emit, "answer", {"text": answer, "citations": citations})

        if mode == "copilot" and not req.dry_run:
            ticket = await self._file_ticket(answer, parsed, technician_id, delta, manual, run_id, executor, llm_info)
            tool_ctx.ticket = ticket
            if ticket:
                await _safe_emit(emit, "ticket", {"ticket": ticket})
            if parsed.complete and parsed.unit_id and ticket:
                await _safe_emit(emit, "retain", {"status": "sending", "source": "hindsight", "operation_id": None, "error": None})
            retain_view = await self._retain_session(parsed, technician, ticket, delta, run_id, warnings)
            if memory_view is not None:
                memory_view["retain"] = retain_view
            if retain_view:
                await _safe_emit(emit, "retain", {k: retain_view.get(k) for k in ("status", "source", "operation_id", "error")})

        timings["total_ms"] = int((time.perf_counter() - started) * 1000)
        return {
            "run_id": run_id,
            "mode": mode,
            "technician": technician,
            "parsed": parsed.to_dict(),
            "manual": manual,
            "answer": answer,
            "memory": memory_view,
            "tool_calls": tool_ctx.trace,
            "citations": citations,
            "ticket": tool_ctx.ticket,
            "llm": llm_info,
            "timings": timings,
            "warnings": warnings,
            "created_at": now_iso(),
        }

    async def confirm_outcome(self, ticket_id: str, *, outcome_held: bool, notes: str, technician_id: str | None) -> dict[str, Any]:
        """Close the learning loop: the technician reports whether the recommended fix held."""
        existing = self.tickets.get(ticket_id)
        if existing is None:
            raise KeyError(ticket_id)
        tech = technician_id or existing.get("technician_id") or "Tech_Alex"
        ticket = self.tickets.confirm_outcome(ticket_id, outcome_held=outcome_held, notes=notes, confirmed_by=tech)
        model = self.catalog.models[ticket["model"]]
        spec = self.catalog.error_spec(ticket["model"], ticket["error_code"]) or {"title": ""}
        verdict = "HELD" if outcome_held else "did NOT hold"
        record = RepairRecord(
            record_id=f"{ticket_id.lower()}-outcome",
            record_type="outcome",
            occurred_at=ticket["outcome_confirmed_at"],
            technician_id=tech,
            technician_role=self.catalog.technician_role(tech),
            unit_id=ticket["unit_id"],
            site=self.catalog.site_name(ticket["unit_id"]),
            model_key=ticket["model"],
            model_name=model["name"],
            fleet=model["fleet"],
            error_code=ticket["error_code"],
            error_title=spec["title"],
            action_taken=ticket.get("canonical_action") or ticket["recommended_action"],
            action_category=ticket.get("action_category", "manual"),
            outcome_held=outcome_held,
            root_cause=ticket.get("diagnosis", "unconfirmed") if outcome_held else "unconfirmed",
            notes=(
                f"{tech} confirmed that the fix I recommended on ticket {ticket_id} for {ticket['unit_id']} "
                f"{ticket['error_code']} ({ticket['recommended_action']}) {verdict}. "
                + (f"Technician notes: {notes}" if notes else "")
            ).strip(),
            work_order=ticket_id,
            visit=1,
            knowledge_source="copilot_confirmed",
        )
        retain = await self.memory.retain_interaction(
            resolve_bank_id(model["fleet"]), record.technician_id, record.unit_id, record.error_code,
            record.action_taken, record.outcome_held, record=record, run_id=ticket.get("run_id"), wait_s=RETAIN_WAIT_S,
        )
        return {"ticket": ticket, "retain": retain.to_dict(), "record": record.to_retain_item()}

    async def reflect(self, model_key: str, error_code: str, *, force: bool = False) -> dict[str, Any]:
        model = self.catalog.models[model_key]
        spec = self.catalog.error_spec(model_key, error_code)
        if spec is None:
            raise KeyError(f"{model_key}/{error_code}")
        result = await self.memory.reflect_patterns(
            resolve_bank_id(model["fleet"]), model_key, error_code,
            model_name=model["name"], error_title=spec["title"], use_cache=not force,
        )
        return result.to_dict()

    def prefetch(self, query: str, *, unit_hint: str | None, technician_hint: str | None) -> dict[str, Any]:
        """Warm the recall cache while the technician is still typing."""
        parsed = self.catalog.parse(query, unit_hint=unit_hint, technician_hint=technician_hint)
        if not parsed.complete:
            return {"started": 0, "parsed": parsed.to_dict()}
        bodies = [self.memory.outcome_recall_body(parsed.model_key, parsed.error_code, parsed.error_title or "",
                                                  parsed.model_name or "")]
        if parsed.unit_id:
            query_text, tags, kwargs = self._unit_recall_args(parsed.unit_id)
            bodies.append(self.memory.recall_body(query_text, tags, **kwargs))
        started = self.memory.prefetch(resolve_bank_id(parsed.fleet), bodies)
        if parsed.unit_id:
            for bank, body in self.notes.recall_bodies(parsed.unit_id):
                started += self.memory.prefetch(bank, [body])
        return {"started": started, "parsed": parsed.to_dict()}

    def reflection_candidates(self) -> list[dict[str, str]]:
        """Model/error pairs whose memory contradicts the manual: worth prewarming a reflection for."""
        pairs = []
        for model_key, model in self.catalog.models.items():
            for code, spec in model["error_codes"].items():
                summary = summarize_outcomes(self.memory.fallback.outcome_hits(model_key, code))
                primary = spec["manual"]["primary_action"]
                manual_held = next((s.held for s in summary.manual if s.action == primary), 0)
                if should_reflect(summary, manual_held)[0]:
                    pairs.append({"bank_id": resolve_bank_id(model["fleet"]), "model_key": model_key, "error_code": code,
                                  "model_name": model["name"], "error_title": spec["title"]})
        return pairs

    # ============================================================== memory phase
    @staticmethod
    def _unit_recall_args(unit_id: str) -> tuple[str, list[str], dict[str, Any]]:
        return (
            f"Service history of unit {unit_id}: recurring faults, prior repairs and whether they held.",
            [unit_tag(unit_id)],
            {"budget": "low", "max_tokens": 1500, "tags_match": "any_strict"},
        )

    async def _gather_memory(
        self, query: str, parsed: ParsedQuery, run_id: str, emit: Emit | None
    ) -> tuple[dict[str, Any], list[MemoryHit], dict[str, Any] | None, str | None, dict[str, int]]:
        bank_id = resolve_bank_id(parsed.fleet)
        started = time.perf_counter()
        await _safe_emit(emit, "recall", {"status": "start", "bank_id": bank_id})

        # One recall feeds both the prompt and the hold-rate statistics; the unit recall runs alongside.
        calls: list[Any] = []
        if parsed.complete:
            calls.append(self.memory.fix_outcome_hits(
                bank_id, parsed.model_key, parsed.error_code, error_title=parsed.error_title or "",
                model_name=parsed.model_name or "", run_id=run_id))
        else:
            calls.append(self.memory.recall_context(bank_id, query, None, run_id=run_id, label="open recall"))
        if parsed.unit_id:
            query_text, tags, kwargs = self._unit_recall_args(parsed.unit_id)
            calls.append(self.memory.recall_context(bank_id, query_text, tags, run_id=run_id, limit=8,
                                                    label="unit history", **kwargs))
        # The site briefing (field notes) is recalled alongside, so it adds no latency.
        briefing_call = [self.notes.briefing(parsed.unit_id, run_id=run_id)] if parsed.unit_id else []
        results = await asyncio.gather(*calls, *briefing_call)
        briefing = results[-1] if briefing_call else None
        if briefing_call:
            results = results[:-1]
            await _safe_emit(emit, "briefing", {"count": len(briefing["items"]), "source": briefing["source"],
                                                "items": briefing["items"][:5], "site": briefing["site"]})

        if parsed.complete:
            stats_hits, fleet_recall, stats_source = results[0]
        else:
            fleet_recall, stats_hits, stats_source = results[0], [], "none"
        unit_recall: RecallOutcome | None = results[1] if parsed.unit_id else None
        recall_outcomes = [fleet_recall] + ([unit_recall] if unit_recall else [])
        hits = select_prompt_hits(fleet_recall.hits, unit_recall.hits if unit_recall else [])
        timings = {"recall_ms": int((time.perf_counter() - started) * 1000)}

        delta: dict[str, Any] | None = None
        reflection_dict: dict[str, Any] | None = None
        reflection_text: str | None = None
        if parsed.complete:
            summary = summarize_outcomes(stats_hits, parsed.unit_id)
            manual = self.catalog.manual(parsed.model_key, parsed.error_code)
            delta = memory_delta(summary, manual["primary_action"] if manual else None, stats_source, parsed.site)

        sources = {r.source for r in recall_outcomes}
        source = "hindsight" if sources == {"hindsight"} else ("local_fallback" if sources == {"local_fallback"} else "mixed")
        await _safe_emit(emit, "recall", {
            "status": "done", "source": source, "count": len(hits), "latency_ms": timings["recall_ms"],
            "recalls": [{"label": r.label, "source": r.source, "status": r.status, "latency_ms": r.latency_ms,
                         "hits": len(r.hits)} for r in recall_outcomes],
            "delta": delta,
        })

        if parsed.complete and delta is not None:
            do_reflect, trigger = should_reflect(summarize_outcomes(stats_hits), delta["manual_held"])
            if do_reflect:
                reflect_started = time.perf_counter()
                reflection = await self.memory.reflect_patterns(
                    bank_id, parsed.model_key, parsed.error_code, model_name=parsed.model_name or "",
                    error_title=parsed.error_title or "", run_id=run_id, wait_s=REFLECT_WAIT_S,
                )
                timings["reflect_ms"] = int((time.perf_counter() - reflect_started) * 1000)
                # The local synthesis restates the outcome statistics already in the prompt; only
                # Hindsight's own synthesis adds information worth the tokens.
                reflection_text = reflection.text if reflection.source == "hindsight" else None
                reflection_dict = {**reflection.to_dict(), "trigger": trigger}
                await _safe_emit(emit, "reflect", {"status": reflection.status, "source": reflection.source,
                                                   "cached": reflection.cached, "trigger": trigger})

        view = {
            "bank_id": bank_id,
            "source": source,
            "recalls": [r.to_dict() for r in recall_outcomes],
            "hits": [h.to_dict() for h in hits],
            "delta": delta,
            "reflection": reflection_dict,
            "briefing": briefing,
            "retain": None,
        }
        return view, hits, delta, reflection_text, timings

    # ================================================================ LLM phase
    async def _tool_loop(
        self, mode: Mode, model: str, context: str, req: DiagnoseRequest, executor: ToolExecutor,
        info: dict[str, Any], emit: Emit | None,
    ) -> str:
        if not self.llm.available:
            raise LLMUnavailable("GROQ_API_KEY is not configured")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": COPILOT_SYSTEM if mode == "copilot" else BASELINE_SYSTEM},
            {"role": "system", "content": f"CONTEXT\n{context}"},
            *_clean_history(req.history.get(mode, [])),
            {"role": "user", "content": req.query},
        ]
        tools = tool_specs([t for t in executor.ctx.allowed if t != TICKET_TOOL])
        usage: dict[str, int] = info["usage"]

        async def on_wait(seconds: float, reason: str) -> None:
            info["waits"].append({"seconds": round(seconds, 1), "reason": reason})
            await _safe_emit(emit, "llm_wait", {"seconds": round(seconds, 1), "reason": reason})

        async def complete(tool_choice: str) -> Any:
            turn = await self.llm.complete(messages, tools=tools, tool_choice=tool_choice, model=model,
                                           max_tokens=1400, on_wait=on_wait)
            for k, v in turn.usage.items():
                usage[k] = usage.get(k, 0) + (v or 0)
            info["usage"] = usage
            return turn

        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            final_round = round_no == MAX_TOOL_ROUNDS
            await _safe_emit(emit, "llm", {"round": round_no, "status": "thinking", "model": model})
            turn = await complete("none" if final_round else "auto")
            info["rounds"] = round_no
            if turn.recovered_from:
                info["recoveries"].append({"round": round_no, "kind": turn.recovered_from})
            if not turn.tool_calls or final_round:
                if turn.content:
                    return turn.content
                break
            messages.append(turn.assistant_message())
            for call in turn.tool_calls:
                await _safe_emit(emit, "tool", {"phase": "start", "name": call.name, "arguments": call.arguments,
                                                "repaired": call.repaired, "repair_notes": call.repair_notes})
            for call, result in await executor.execute_all(turn.tool_calls):
                messages.append(tool_result_message(call, result))
                trace = next((t for t in reversed(executor.ctx.trace) if t["id"] == call.id), None)
                if trace:
                    await _safe_emit(emit, "tool", {"phase": "done", **trace, "result": None})

        # The model stopped without prose (or ran out of rounds): ask once more, tools disabled.
        messages.append({"role": "user", "content": "Write the final answer now using the required sections."})
        info["rounds"] += 1
        turn = await complete("none")
        if not turn.content:
            raise LLMUnavailable("model returned an empty answer")
        return turn.content

    # ============================================================ post-run phase
    async def _file_ticket(
        self, answer: str, parsed: ParsedQuery, technician_id: str, delta: dict[str, Any] | None,
        manual: dict[str, Any] | None, run_id: str, executor: ToolExecutor, info: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Turn the answer into a structured work order with one small, forced function call.

        It runs after the answer is already on screen, costs a few hundred tokens instead of a
        second full-context round, and falls back to a deterministic ticket if the LLM is unavailable."""
        if not (parsed.complete and parsed.unit_id):
            return None
        if self.llm.available and not info.get("error"):
            known = [f["action"] for f in (delta or {}).get("field_fixes", [])[:2]] + ([manual["primary_action"]] if manual else [])
            messages = [
                {"role": "system", "content": (
                    "You file repair work orders. Call log_repair_ticket exactly once using only facts from the "
                    "diagnosis. recommended_action is the first CORRECTIVE action the diagnosis recommends (the repair "
                    "itself, never a lockout/tagout, isolation or inspection step); prefer the wording of a known fix "
                    "when it matches. action_category is 'field' for a field-verified fix and 'manual' for an OEM "
                    "manual step.")},
                {"role": "user", "content": (
                    f"Unit {parsed.unit_id}, error {parsed.error_code}. Known fixes: {'; '.join(known) or 'n/a'}.\n\n"
                    f"Diagnosis:\n{answer[:2500]}")},
            ]
            try:
                turn = await self.llm.complete(
                    messages, tools=tool_specs([TICKET_TOOL]), model=self.llm.model, max_tokens=600,
                    tool_choice={"type": "function", "function": {"name": TICKET_TOOL}},  # type: ignore[arg-type]
                )
                for k, v in turn.usage.items():
                    info["usage"][k] = info["usage"].get(k, 0) + (v or 0)
                for call in turn.tool_calls[:1]:
                    if call.name == TICKET_TOOL:
                        call.arguments.setdefault("unit_id", parsed.unit_id)
                        call.arguments.setdefault("error_code", parsed.error_code)
                        await executor.execute(call, stage="work order")
            except LLMUnavailable as exc:
                log.warning("work-order extraction failed: %s", exc)
            if executor.ctx.ticket:
                return executor.ctx.ticket
        return self._auto_ticket(parsed, technician_id, delta, manual, answer, run_id)

    def _auto_ticket(
        self, parsed: ParsedQuery, technician_id: str, delta: dict[str, Any] | None,
        manual: dict[str, Any] | None, answer: str, run_id: str,
    ) -> dict[str, Any] | None:
        """Guarantee a ticket exists so the outcome can be confirmed, even if the model skipped the tool."""
        if not (parsed.complete and parsed.unit_id):
            return None
        best = (delta or {}).get("field_fixes") or []
        if delta and delta.get("has_delta") and best:
            action, category, diagnosis = best[0]["action"], "field", best[0]["root_cause"] or best[0]["action"]
        else:
            action = manual["primary_action"] if manual else "Follow OEM procedure"
            category, diagnosis = "manual", "; ".join((manual or {}).get("probable_causes", [])[:1]) or "unconfirmed"
        return self.tickets.create(
            unit_id=parsed.unit_id, model=parsed.model_key, error_code=parsed.error_code, technician_id=technician_id,
            diagnosis=diagnosis[:600], recommended_action=action[:400], canonical_action=action, action_category=category,
            priority="normal", parts=[], run_id=run_id, created_via="auto", answer_excerpt=answer[:400],
        )

    async def _retain_session(
        self, parsed: ParsedQuery, technician: dict[str, Any], ticket: dict[str, Any] | None,
        delta: dict[str, Any] | None, run_id: str, warnings: list[str],
    ) -> dict[str, Any] | None:
        if not (parsed.complete and parsed.unit_id and ticket):
            warnings.append("No unit identified, so this session was not retained to memory.")
            return None
        headline = (delta or {}).get("headline") or "no prior field history contradicted the manual."
        record = RepairRecord(
            record_id=f"{ticket['id'].lower()}-diagnosis",
            record_type="diagnosis",
            occurred_at=now_iso(),
            technician_id=technician["id"],
            technician_role=technician.get("role", "Technician"),
            unit_id=parsed.unit_id,
            site=parsed.site or "",
            model_key=parsed.model_key or "",
            model_name=parsed.model_name or "",
            fleet=parsed.fleet or "",
            error_code=parsed.error_code or "",
            error_title=parsed.error_title or "",
            action_taken=ticket.get("canonical_action") or ticket["recommended_action"],
            action_category=ticket.get("action_category", "manual"),
            outcome_held=None,
            root_cause=ticket.get("diagnosis", "unconfirmed"),
            notes=(
                f"I (Field Service Copilot) advised {technician['id']} on {parsed.unit_id} {parsed.error_code}. "
                f"Memory showed: {headline} Suspected root cause: {ticket.get('diagnosis')}. "
                f"Recommended first action: {ticket['recommended_action']}. Awaiting field confirmation on ticket {ticket['id']}."
            ),
            work_order=ticket["id"],
            knowledge_source="copilot",
        )
        retain = await self.memory.retain_interaction(
            resolve_bank_id(parsed.fleet), record.technician_id, record.unit_id, record.error_code,
            record.action_taken, record.outcome_held, record=record, run_id=run_id, wait_s=RETAIN_WAIT_S,
        )
        return {**retain.to_dict(), "record": record.to_retain_item()}
