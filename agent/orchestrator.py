"""Diagnostic loop: Input -> Recall -> (Reflect) -> LLM tool loop -> Retain.

Two agents share the same LLM and tools so the comparison is fair:
* baseline: OEM manual + telemetry only, no memory, nothing retained
* copilot:  adds Hindsight recall/reflect, outcome verification, ticketing and post-run retain
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.groq_client import GroqClient, LLMUnavailable
from agent.prompts import BASELINE_SYSTEM, COPILOT_SYSTEM, build_context, render_fallback_brief
from agent.tools import BASELINE_TOOLS, COPILOT_TOOLS, ToolContext, ToolExecutor, tool_result_message, tool_specs
from fleet.catalog import Catalog, ParsedQuery
from fleet.tickets import TicketLedger
from memory.bank_schemas import RepairRecord, context_tags, now_iso, resolve_bank_id, unit_tag
from memory.hindsight_wrapper import HindsightMemory, RecallOutcome
from memory.insights import MemoryHit, memory_delta, summarize_outcomes

log = logging.getLogger("copilot.agent")

Mode = Literal["baseline", "copilot"]
MAX_TOOL_ROUNDS = 6
MAX_HISTORY_TURNS = 6


@dataclass
class DiagnoseRequest:
    query: str
    technician_id: str = "Tech_Alex"
    unit_id: str | None = None
    # Conversation so far, per pane: {"baseline": [...], "copilot": [...]}
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _new_run_id(mode: str) -> str:
    return f"{mode[:4]}-{secrets.token_hex(4)}"


def _clean_history(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns = [
        {"role": h["role"], "content": str(h.get("content", ""))[:2000]}
        for h in history or []
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
    ]
    return turns[-MAX_HISTORY_TURNS:]


def _merge_hits(primary: list[MemoryHit], *secondary: list[MemoryHit], limit: int = 12, primary_share: int = 8) -> list[MemoryHit]:
    """Fleet-history hits first, then unit-history hits, de-duplicated by source document."""
    seen: set[str] = set()
    merged: list[MemoryHit] = []

    def take(hits: list[MemoryHit], cap: int) -> None:
        for hit in hits:
            key = hit.document_id or hit.id
            if len(merged) >= cap or key in seen:
                continue
            seen.add(key)
            merged.append(hit)

    take(primary, primary_share if secondary else limit)
    for group in secondary:
        take(group, limit)
    take(primary, limit)  # backfill if the secondary recalls came back thin
    return merged


class DiagnosticOrchestrator:
    def __init__(self, *, llm: GroqClient, memory: HindsightMemory, catalog: Catalog, tickets: TicketLedger) -> None:
        self.llm = llm
        self.memory = memory
        self.catalog = catalog
        self.tickets = tickets

    # ================================================================ public API
    async def diagnose(self, req: DiagnoseRequest, mode: str) -> dict[str, Any]:
        if mode == "compare":
            baseline, copilot = await asyncio.gather(self.run(req, "baseline"), self.run(req, "copilot"))
            return {"mode": "compare", "baseline": baseline, "copilot": copilot}
        if mode not in ("baseline", "copilot"):
            raise ValueError(f"unknown mode '{mode}'")
        return {"mode": mode, mode: await self.run(req, mode)}  # type: ignore[arg-type]

    async def run(self, req: DiagnoseRequest, mode: Mode) -> dict[str, Any]:
        started = time.perf_counter()
        run_id = _new_run_id(mode)
        parsed = self.catalog.parse(req.query, unit_hint=req.unit_id, technician_hint=req.technician_id)
        technician_id = parsed.technician_id or req.technician_id
        technician = self.catalog.technicians.get(technician_id, {"id": technician_id, "role": "Technician"})
        manual = self.catalog.manual(parsed.model_key, parsed.error_code) if parsed.complete else None
        warnings: list[str] = []
        if not parsed.complete:
            warnings.append("Could not identify both the equipment model and a known error code from the question.")

        memory_view: dict[str, Any] | None = None
        hits: list[MemoryHit] | None = None
        delta: dict[str, Any] | None = None
        reflection_text: str | None = None
        timings: dict[str, int] = {}

        if mode == "copilot":
            memory_view, hits, delta, reflection_text, timings = await self._gather_memory(req.query, parsed, run_id)

        context = build_context(
            technician=technician, parsed=parsed.to_dict(), manual=manual, hits=hits, delta=delta,
            reflection=reflection_text, memory_source=(memory_view or {}).get("source"),
        )
        tool_ctx = ToolContext(
            run_id=run_id, technician_id=technician_id, catalog=self.catalog,
            memory=self.memory if mode == "copilot" else None,
            tickets=self.tickets if mode == "copilot" else None,
            allowed=COPILOT_TOOLS if mode == "copilot" else BASELINE_TOOLS,
        )

        llm_started = time.perf_counter()
        llm_info: dict[str, Any] = {"model": self.llm.model, "rounds": 0, "usage": {}, "recoveries": []}
        try:
            answer = await self._tool_loop(mode, context, req, tool_ctx, llm_info)
        except LLMUnavailable as exc:
            warnings.append(f"LLM unavailable: {exc}")
            llm_info["error"] = str(exc)
            answer = render_fallback_brief(mode=mode, parsed=parsed.to_dict(), manual=manual, delta=delta, reason=str(exc))
        timings["llm_ms"] = int((time.perf_counter() - llm_started) * 1000)

        retain_view = None
        if mode == "copilot":
            ticket = tool_ctx.ticket or self._auto_ticket(parsed, technician_id, delta, manual, answer, run_id)
            tool_ctx.ticket = ticket
            retain_view = await self._retain_session(parsed, technician, ticket, delta, run_id, warnings)
            if memory_view is not None:
                memory_view["retain"] = retain_view

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
            record.action_taken, record.outcome_held, record=record, run_id=ticket.get("run_id"),
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

    # ============================================================== memory phase
    async def _gather_memory(
        self, query: str, parsed: ParsedQuery, run_id: str
    ) -> tuple[dict[str, Any], list[MemoryHit], dict[str, Any] | None, str | None, dict[str, int]]:
        bank_id = resolve_bank_id(parsed.fleet)
        started = time.perf_counter()

        recalls: list[Any] = []
        if parsed.complete:
            fleet_query = (
                f"{parsed.model_name} {parsed.error_code} {parsed.error_title}: root causes found in the field, "
                f"OEM manual procedures that failed, fixes that held. Technician question: {query}"
            )
            recalls.append(self.memory.recall_context(
                bank_id, fleet_query, context_tags(parsed.model_key, parsed.error_code), run_id=run_id, label="fleet history"))
        else:
            recalls.append(self.memory.recall_context(bank_id, query, None, run_id=run_id, label="open recall"))
        if parsed.unit_id:
            recalls.append(self.memory.recall_context(
                bank_id, f"Service history of unit {parsed.unit_id}: recurring faults, prior repairs and whether they held.",
                [unit_tag(parsed.unit_id)], run_id=run_id, tags_match="any_strict", limit=8, label="unit history"))
        extra = [self.memory.fix_outcome_hits(bank_id, parsed.model_key, parsed.error_code, run_id=run_id)] if parsed.complete else []

        # All recalls share one wall-clock budget because they run concurrently.
        results = await asyncio.gather(*recalls, *extra)
        recall_outcomes: list[RecallOutcome] = list(results[: len(recalls)])
        hits = _merge_hits(*(r.hits for r in recall_outcomes))
        timings = {"recall_ms": int((time.perf_counter() - started) * 1000)}

        delta: dict[str, Any] | None = None
        reflection_dict: dict[str, Any] | None = None
        reflection_text: str | None = None
        if extra:
            outcome_hits, _, stats_source = results[-1]
            summary = summarize_outcomes(outcome_hits, parsed.unit_id)
            manual = self.catalog.manual(parsed.model_key, parsed.error_code)
            delta = memory_delta(summary, manual["primary_action"] if manual else None, stats_source, parsed.site)

            # Reflect only when memory contradicts the manual; it is the expensive call.
            manual_failures = sum(s.attempts - s.held for s in summary.manual)
            best_field = summary.field[0] if summary.field else None
            contradicts_manual = bool(best_field and best_field.held >= 3 and best_field.held > delta["manual_held"])
            if manual_failures >= 2 or contradicts_manual:
                reflect_started = time.perf_counter()
                reflection = await self.memory.reflect_patterns(
                    bank_id, parsed.model_key, parsed.error_code,
                    model_name=parsed.model_name or "", error_title=parsed.error_title or "", run_id=run_id,
                )
                timings["reflect_ms"] = int((time.perf_counter() - reflect_started) * 1000)
                reflection_text = reflection.text
                trigger = f"OEM steps failed {manual_failures}× in memory" if manual_failures >= 2 else "field fix outperforms the manual"
                reflection_dict = {**reflection.to_dict(), "trigger": trigger}

        sources = {r.source for r in recall_outcomes}
        view = {
            "bank_id": bank_id,
            "source": "hindsight" if sources == {"hindsight"} else ("local_fallback" if sources == {"local_fallback"} else "mixed"),
            "recalls": [r.to_dict() for r in recall_outcomes],
            "hits": [h.to_dict() for h in hits],
            "delta": delta,
            "reflection": reflection_dict,
            "retain": None,
        }
        return view, hits, delta, reflection_text, timings

    # ================================================================ LLM phase
    async def _tool_loop(
        self, mode: Mode, context: str, req: DiagnoseRequest, tool_ctx: ToolContext, info: dict[str, Any]
    ) -> str:
        if not self.llm.available:
            raise LLMUnavailable("GROQ_API_KEY is not configured")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": COPILOT_SYSTEM if mode == "copilot" else BASELINE_SYSTEM},
            {"role": "system", "content": f"Context for this diagnosis:\n\n{context}"},
            *_clean_history(req.history.get(mode, [])),
            {"role": "user", "content": req.query},
        ]
        tools = tool_specs(tool_ctx.allowed)
        executor = ToolExecutor(tool_ctx)
        usage: dict[str, int] = {}

        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            final_round = round_no == MAX_TOOL_ROUNDS
            turn = await self.llm.complete(messages, tools=tools, tool_choice="none" if final_round else "auto")
            info["rounds"] = round_no
            for k, v in turn.usage.items():
                usage[k] = usage.get(k, 0) + v
            info["usage"] = usage
            if turn.recovered_from:
                info["recoveries"].append({"round": round_no, "kind": turn.recovered_from})
            if not turn.tool_calls or final_round:
                if turn.content:
                    return turn.content
                break
            messages.append(turn.assistant_message())
            for call, result in await executor.execute_all(turn.tool_calls):
                messages.append(tool_result_message(call, result))

        # The model stopped without prose (or ran out of rounds): ask once more, tools disabled.
        messages.append({"role": "user", "content": "Write the final answer now using the required sections."})
        turn = await self.llm.complete(messages, tools=tools, tool_choice="none")
        if not turn.content:
            raise LLMUnavailable("model returned an empty answer")
        return turn.content

    # ============================================================ post-run phase
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
            priority="normal",
            parts=[], run_id=run_id, created_via="auto", answer_excerpt=answer[:400],
        )

    async def _retain_session(
        self, parsed: ParsedQuery, technician: dict[str, Any], ticket: dict[str, Any] | None,
        delta: dict[str, Any] | None, run_id: str, warnings: list[str],
    ) -> dict[str, Any] | None:
        if not (parsed.complete and parsed.unit_id and ticket):
            warnings.append("No unit identified, so this session was not retained to memory.")
            return None
        headline = (delta or {}).get("headline") or "no prior field history contradicted the manual"
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
            record.action_taken, record.outcome_held, record=record, run_id=run_id,
        )
        return {**retain.to_dict(), "record": record.to_retain_item()}
