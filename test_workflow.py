"""End-to-end workflow tests. No browser, no keys, no network required.

    python test_workflow.py            # offline: scripted LLM + mock Hindsight API (deterministic)
    python test_workflow.py --live     # real Groq + Hindsight using keys from .env
    pytest test_workflow.py            # same offline checks under pytest

The offline run drives the real FastAPI app, orchestrator, tools, Hindsight wrapper,
retain delivery, outbox and JSON repair. Only the two network boundaries are
replaced: Groq by a scripted model that deliberately misbehaves (429s, malformed and
text-embedded tool calls), and Hindsight by an in-process mock of its REST API with
configurable latency and outages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import openai

from agent.citations import normalize_markdown, verify_citations
from agent.groq_client import GroqClient, TokenBucket, repair_json
from config import Settings, settings
from main import build_services, create_app
from memory.bank_schemas import RepairRecord, load_seed, now_iso, records_from_seed
from memory.event_log import EventLog
from fleet.catalog import get_catalog
from memory.fallback_store import LocalMemoryStore
from memory.field_notes import screen_note, seed_notes
from memory.hindsight_wrapper import RETAIN_RETRIES, HindsightMemory

QUERY_B = (
    "Tech_Alex here at Riverside Medical. Carrier AquaForce 30XA unit CHL-0417 is tripping E-412 again. "
    "The manual says replace the inverter board. Should I?"
)
FOLLOW_UP = "Tech_Sarah on Carrier AquaForce 30XA CHL-0631, E-412 overcurrent trips at start-up. Board or something else?"
_UNIT_IN_PROMPT = re.compile(r"\b[Uu]nit ((?:CHL|INV|ELV)-\d{4})")


# =============================================================== test doubles
class MockHindsight:
    """In-process Hindsight REST API backed by the seed data plus whatever gets retained.

    `retain_latency_s` makes writes slower than the 1.5 s recall budget, and `fail_retains`
    simulates an outage (503 with a short Retry-After), which is exactly what the retain path must survive."""

    def __init__(self, api_key: str, *, slow_recall_s: float = 0.0, retain_latency_s: float = 0.0) -> None:
        self.api_key = api_key
        self.slow_recall_s = slow_recall_s
        self.retain_latency_s = retain_latency_s
        self.fail_retains = False
        # A seeded bank: the repair history plus the technicians' seeded field notes.
        self.store = LocalMemoryStore(records_from_seed(load_seed()) + seed_notes(get_catalog()),
                                      Path(tempfile.mkdtemp()) / "mock.jsonl")
        self.retained: list[dict[str, Any]] = []
        self.directives: list[dict[str, Any]] = []
        self.deleted: set[str] = set()  # documents removed through DELETE /documents/{id}
        self.calls: list[str] = []

    def _recall(self, body: dict[str, Any]) -> dict[str, Any]:
        strict = body.get("tags_match") in ("all_strict", "any_strict")
        hits = [h for h in self.store.search(body["query"], body.get("tags"), limit=60 if strict else 12)
                if h.document_id not in self.deleted]
        if body.get("tags_match") == "all_strict":
            hits = [h for h in hits if all(t in h.tags for t in body.get("tags", []))]
        if body.get("tags_match") == "any_strict":
            hits = [h for h in hits if any(t in h.tags for t in body.get("tags", []))]
        return {"results": [
            {"id": f"fact-{h.id}", "text": h.text, "type": h.type, "tags": h.tags, "metadata": h.metadata,
             "document_id": h.document_id, "occurred_start": h.occurred_at, "scores": {"final": h.score}}
            for h in hits
        ]}

    def _retain(self, body: dict[str, Any], bank: str) -> dict[str, Any]:
        for item in body["items"]:
            meta = item["metadata"]
            if meta.get("record_type") == "note":
                self.store.add(RepairRecord(
                    record_id=item["document_id"], record_type="note", occurred_at=meta["occurred_at"],
                    technician_id=meta["technician"], technician_role=meta["technician_role"], unit_id=meta["unit_id"],
                    site=meta["site"], model_key=meta["model"], model_name="", fleet="", error_code="", error_title="",
                    action_taken="", action_category="unknown", outcome_held=None, root_cause="",
                    notes=meta["note_text"], note_kind=meta["note_kind"],
                ))
            elif meta.get("record_type") in ("repair", "diagnosis", "outcome"):
                self.store.add(RepairRecord(
                    record_id=item["document_id"], record_type=meta["record_type"], occurred_at=meta["occurred_at"],
                    technician_id=meta["technician"], technician_role=meta["technician_role"], unit_id=meta["unit_id"],
                    site=meta["site"], model_key=meta["model"], model_name=meta["model_name"], fleet="chillers",
                    error_code=meta["error_code"], error_title="", action_taken=meta["action_taken"],
                    action_category=meta["action_category"],
                    outcome_held={"true": True, "false": False}.get(meta["outcome_held"]), root_cause=meta["root_cause"],
                    notes=item["content"], work_order=meta["work_order"],
                ))
        self.retained.extend(body["items"])
        return {"success": True, "bank_id": bank, "items_count": len(body["items"]),
                "async": body.get("async", False), "operation_id": f"op-{len(self.retained)}"}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        body = json.loads(request.content or b"{}")
        bank = path.split("/")[4] if path.startswith("/v1/default/banks/") else ""
        if path.endswith("/memories/recall"):
            if self.slow_recall_s:
                await asyncio.sleep(self.slow_recall_s)
            return httpx.Response(200, json=self._recall(body))
        if path.endswith("/memories") and method == "POST":
            if self.retain_latency_s:
                await asyncio.sleep(self.retain_latency_s)
            if self.fail_retains:
                return httpx.Response(503, headers={"retry-after": "0.1"}, json={"detail": "temporarily unavailable"})
            return httpx.Response(200, json=self._retain(body, bank))
        if path.endswith("/reflect"):
            if "response_schema" in body:
                return httpx.Response(200, json={"text": "", "structured_output": {
                    "title": "30XA E-412: re-terminate the J4 harness before replacing the board",
                    "symptom": "Compressor A1 overcurrent trips at start-up",
                    "root_cause": "Loose, vibration-chafed J4 power harness at the VFD terminal block",
                    "recommended_procedure": ["Apply lockout/tagout and verify < 50 V DC.", "Re-terminate the J4 harness with new ferrules (2.5 N·m).",
                                              "Fit a strain-relief bracket.", "If the fault persists, replace the inverter board."],
                    "when_to_use_oem_procedure": "When the harness is sound, as on CHL-0417 on 2026-09-16.",
                    "site_conditions": "High-vibration plant rooms", "confidence": "high"},
                    "based_on": {"memories": [{"id": "f1"}], "directives": [{"name": d["name"]} for d in self.directives]}})
            return httpx.Response(200, json={
                "text": "E-412 on the 30XA is usually a loose J4 harness (Tech_Dave, 2026-09-03), not the board.",
                "based_on": {"memories": [{"id": "fact-1", "text": "J4 harness finding", "type": "world"}],
                             "directives": [{"name": d["name"]} for d in self.directives]},
            })
        if path.endswith("/directives") and method == "GET":
            return httpx.Response(200, json={"items": self.directives, "total": len(self.directives)})
        if path.endswith("/directives") and method == "POST":
            self.directives.append({**body, "id": f"d{len(self.directives) + 1}"})
            return httpx.Response(200, json=self.directives[-1])
        if "/documents/" in path and method == "DELETE":
            doc = path.rsplit("/", 1)[1]
            known = doc in {r.record_id for r in self.store.all_records()} or doc in {i["document_id"] for i in self.retained}
            if not known or doc in self.deleted:
                return httpx.Response(404, json={"detail": "document not found"})
            self.deleted.add(doc)
            return httpx.Response(200, json={"success": True, "message": "deleted", "document_id": doc, "memory_units_deleted": 3})
        if "/directives/" in path and method == "DELETE":
            directive_id = path.rsplit("/", 1)[1]
            self.directives = [d for d in self.directives if d.get("id") != directive_id]
            return httpx.Response(200, json={"success": True})
        if "/directives/" in path and method == "PATCH":
            return httpx.Response(200, json=body)
        if path.endswith("/config") or (method == "PUT" and path.count("/") == 4):
            return httpx.Response(200, json={"bank_id": bank})
        if path.endswith("/stats"):
            return httpx.Response(200, json={"total_nodes": self.store.size, "total_documents": self.store.size,
                                             "total_observations": 0, "pending_operations": 0})
        return httpx.Response(404, json={"detail": f"not found: {method} {path}"})


class ScriptedGroq(GroqClient):
    """Stands in for Groq at the raw-API boundary, so parsing, repair and retries run for real.

    Copilot script: one 429, then a Groq `tool_use_failed` whose `failed_generation` carries the first
    call, a namespaced tool name with python-dict arguments, a tool call written into the text, and a
    final answer (bold-wrapped heading, typographic hyphens) built from the tool results. The forced
    work-order call answers with double-encoded arguments and a paraphrased fix; the baseline answers
    straight from the manual. Every response carries Groq's token-budget headers."""

    def __init__(self, cfg: Settings) -> None:
        super().__init__(cfg, client=object())  # any non-None client marks the LLM available
        self.requests: list[dict[str, Any]] = []
        self._faults = ["rate_limit", "tool_use_failed"]

    def _reply(self, message: dict[str, Any], finish_reason: str) -> tuple[dict[str, Any], dict[str, str]]:
        raw = {"choices": [{"finish_reason": finish_reason, "message": {"role": "assistant", **message}}],
               "usage": {"prompt_tokens": 1100, "completion_tokens": 200, "total_tokens": 1300}, "model": self.model}
        return raw, {"x-ratelimit-limit-tokens": "250000", "x-ratelimit-remaining-tokens": "249000"}

    def _tool_call(self, name: str, arguments: str) -> tuple[dict[str, Any], dict[str, str]]:
        return self._reply({"content": None, "tool_calls": [
            {"id": f"call_{len(self.requests)}", "type": "function", "function": {"name": name, "arguments": arguments}}]},
            "tool_calls")

    def _text(self, content: str) -> tuple[dict[str, Any], dict[str, str]]:
        return self._reply({"content": content}, "stop")

    async def _create(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        messages, tool_choice = kwargs["messages"], kwargs.get("tool_choice")
        kind = ("work order" if isinstance(tool_choice, dict)
                else "copilot" if "Field Service Copilot" in messages[0]["content"] else "baseline")
        self.requests.append({"kind": kind, "tools": [t["function"]["name"] for t in kwargs.get("tools") or []],
                              "context": messages[1]["content"]})
        match = _UNIT_IN_PROMPT.search(messages[1]["content"])  # the CONTEXT, or the work-order prompt
        unit = match.group(1) if match else "CHL-0417"

        if kind == "work order":
            args = {"unit_id": unit, "error_code": "E412",
                    "diagnosis": "Loose, vibration-chafed J4 power harness at the VFD terminal block",
                    "recommended_action": "Inspect and re-terminate the J4 power harness at the VFD terminal block; fit strain relief",
                    "action_category": "field", "priority": "high", "parts": "J4 ferrule kit, strain-relief bracket"}
            return self._tool_call("log_repair_ticket", json.dumps(json.dumps(args)))  # double-encoded
        if kind == "baseline":
            return self._text(
                "### Likely root cause\nFailed inverter drive board per OEM §7.4.\n### Recommended procedure\n"
                "1. Apply lockout/tagout and verify the DC bus is below 50 V.\n2. Check supply imbalance and megger the windings.\n"
                "3. Replace inverter drive board 30XA-VFD-412B.\n### Verify before you leave\nRun compressor A1 at 50% load for 30 minutes."
            )

        oem_check = {"model": "carrier-30xa", "error_code": "E-412", "proposed_action": "replace inverter drive board", "unit_id": unit}
        if self._faults:
            fault = self._faults.pop(0)
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            if fault == "rate_limit":
                response = httpx.Response(429, headers={"retry-after": "0.2"}, request=request)
                raise openai.RateLimitError("rate limited", response=response, body=None)
            raise openai.BadRequestError(
                "tool_use_failed", response=httpx.Response(400, request=request),
                body={"code": "tool_use_failed", "failed_generation": f"<function=verify_fix_outcome>{json.dumps(oem_check)}</function>"},
            )

        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if tool_choice != "none":
            if not results:  # only reached once the injected faults are used up
                return self._tool_call("verify_fix_outcome", json.dumps(oem_check))
            if len(results) == 1:  # a namespaced, channel-tagged tool name with python-dict arguments
                return self._tool_call(
                    "functions.verify_fix_outcome<|channel|>commentary",
                    "{'model': 'carrier-30xa', 'error_code': 'E412', "
                    f"'proposed_action': 're-terminate J4 harness', 'unit_id': '{unit}',}}",
                )
            if len(results) == 2:  # a tool call written into the content instead of tool_calls
                call = {"name": "fetch_unit_telemetry", "arguments": {"unit_id": unit, "window_hours": 72}}
                return self._text(f"<tool_call>{json.dumps(call)}</tool_call>")
        manual, fix = results[0]["matched_history"] or {}, results[1]["matched_history"] or {}
        ruled_out = results[1]["field_pattern_checked_but_not_cause"]
        exception = (f" Exception: the harness was sound and the board had failed ({ruled_out[0]['technician']}, "
                     f"{ruled_out[0]['date']}, {ruled_out[0]['unit_id']}).") if ruled_out else ""
        return self._text(  # U+2011 hyphens and a bold-wrapped heading, as gpt-oss writes them
            "**### Likely root cause**\nA loose, vibration\u2011chafed J4 power harness at the VFD terminal block, not the board.\n"
            "### Recommended procedure\n1. Apply lockout/tagout and verify the DC bus is below 50 V.\n"
            "2. Re\u2011terminate the J4 harness with new ferrules (2.5 N·m) and fit strain relief.\n"
            "3. Only if the harness is sound, follow OEM §7.4 and replace the inverter drive board.\n"
            "### Memory delta: field vs. manual\n"
            f"The harness fix held {fix.get('held')} of {fix.get('attempts')} times ({fix.get('first_confirmed_by')}, "
            f"{fix.get('first_confirmed_on')}, {fix.get('first_unit')}); the board swap held {manual.get('held')} of "
            f"{manual.get('attempts')}.{exception}\n"
            "### Verify before you leave\nRun compressor A1 at 50% load for 30 minutes and log current."
        )


# ==================================================================== helpers
def offline_settings(**overrides: Any) -> Settings:
    """Fake keys, a mock URL and the documented budgets, so a local .env can neither reach real
    services nor change the timings these checks rely on."""
    pinned: dict[str, Any] = dict(
        hindsight_api_key="test-key", hindsight_base_url="https://hindsight.mock", hindsight_timeout_s=1.5,
        hindsight_retain_timeout_s=15.0, hindsight_reflect_timeout_s=45.0, hindsight_prewarm=True,
        groq_api_key="test-key", groq_max_retries=2, app_access_token=None, rate_limit_per_min=0, demo_tools=True,
    )
    return replace(Settings(), **{**pinned, **overrides})


def build_offline_app(**mock_options: Any):
    """The real app wired to the scripted model and the mock Hindsight API, in a fresh runtime dir."""
    cfg = offline_settings()
    mock = MockHindsight(cfg.hindsight_api_key, **mock_options)
    services = build_services(cfg, llm=ScriptedGroq(cfg), runtime_dir=Path(tempfile.mkdtemp()),
                              hindsight_transport=httpx.MockTransport(mock.handler))
    return create_app(cfg, services=services), services, mock


def offline_memory(cfg: Settings, mock: MockHindsight, runtime: Path | None = None) -> HindsightMemory:
    """The real wrapper on its own, pointed at the mock, with its journal and ack ledger (the outbox) in `runtime`."""
    runtime = runtime or Path(tempfile.mkdtemp())
    return HindsightMemory(
        cfg, event_log=EventLog(), transport=httpx.MockTransport(mock.handler), acks_path=runtime / "retain_acks.jsonl",
        fallback=LocalMemoryStore(records_from_seed(load_seed()), runtime / "memory_journal.jsonl"),
    )


async def pending_after_restart(cfg: Settings, mock: MockHindsight, runtime: Path) -> int:
    """What a freshly started process would find in the outbox: journaled records without an ack."""
    memory = offline_memory(cfg, mock, runtime)
    pending = memory.outbox_status()["pending"]
    await memory.aclose()
    return pending


def new_record(record_id: str) -> RepairRecord:
    """A fresh Copilot diagnosis for CHL-0417, shaped like the one the orchestrator retains after a session."""
    return RepairRecord(
        record_id=record_id, record_type="diagnosis", occurred_at=now_iso(), technician_id="Tech_Alex",
        technician_role="Junior", unit_id="CHL-0417", site="Riverside Medical Center", model_key="carrier-30xa",
        model_name="Carrier AquaForce 30XA", fleet="chillers", error_code="E-412",
        error_title="Compressor A1 inverter overcurrent trip",
        action_taken="Re-terminate VFD J4 power harness with new ferrules (2.5 N·m) and fit a strain-relief bracket",
        action_category="field", outcome_held=None, root_cause="Loose, vibration-chafed J4 power harness",
        notes="Offline test session awaiting field confirmation.", work_order=record_id.upper(), knowledge_source="copilot",
    )


@asynccontextmanager
async def running(app):
    """Start the app (bank setup and background loops run as in production) and yield a client for it."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://copilot") as client:
            yield client


async def until(condition: Callable[[], bool], what: str, timeout_s: float) -> None:
    """Wait for background work (a delivery finishing, a late recall landing) without a fixed sleep."""
    deadline = time.perf_counter() + timeout_s
    while not condition():
        if time.perf_counter() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}")
        await asyncio.sleep(0.02)


def retain_attempts(mock: MockHindsight) -> int:
    return sum(call.startswith("POST ") and call.endswith("/memories") for call in mock.calls)


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a text/event-stream body into (event, data) pairs, skipping keep-alive comments."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


# ====================================================================== tests
def test_json_repair() -> None:
    print("\n[1] Tool-argument repair")
    cases = {
        "{'unit_id': 'CHL-0417', 'window_hours': 24,}": {"unit_id": "CHL-0417", "window_hours": 24},
        '"{\\"unit_id\\": \\"CHL-0417\\"}"': {"unit_id": "CHL-0417"},
        '```json\n{"unit_id": "CHL-0417"}\n```': {"unit_id": "CHL-0417"},
        '{unit_id: "CHL-0417", urgent: True}': {"unit_id": "CHL-0417", "urgent": True},
        '{"unit_id": "CHL-0417", "notes": "cut off': {"unit_id": "CHL-0417", "notes": "cut off"},
    }
    for raw, expected in cases.items():
        parsed, notes = repair_json(raw)
        check(parsed == expected, f"{raw[:38]!r:42} → {parsed} ({', '.join(notes) or 'clean'})")


def test_citations() -> None:
    print("\n[2] Citations are checked against the repair ledger")
    answer = normalize_markdown(
        "**### Memory delta: field vs. manual**\n"
        "The harness fix held 7 of 7 (Tech_Dave, 2026\u201109\u201103, CHL\u20110417). "  # U+2011 hyphens, as gpt-oss writes them
        "It also held at Northgate (Tech_Dave, 2026-09-05, CHL-0417) "  # garbled: that visit was on CHL-0522
        "and on an August call (Tech_Alex, 2026-08-20, CHL-0417)."  # before the service history starts
    )
    check(answer.startswith("### Memory delta") and "\u2011" not in answer,
          "markdown normalised: bold-wrapped heading unwrapped, U+2011 hyphens made plain")
    result = verify_citations(answer, records_from_seed(load_seed()))
    by_status = {c["status"]: c for c in result["citations"]}
    check(result["total"] == 3 and result["verified"] == 1, "3 citations found; (Tech_Dave, 2026-09-03, CHL-0417) verified")
    check("CHL-0522" in by_status["unit_mismatch"]["ledger_units"], "garbled unit flagged with the ledger's units for that visit")
    check(by_status["not_found"]["technician"] == "Tech_Alex", "citation of a visit that never happened flagged as not found")


def test_token_bucket() -> None:
    print("\n[3] Groq tokens-per-minute pacing")
    bucket = TokenBucket()
    bucket.observe({"x-ratelimit-limit-tokens": "unlimited", "x-ratelimit-remaining-tokens": "?"})
    check(bucket.available() is None and bucket.reserve(5000) == 0.0, "no (or unparseable) budget headers: never waits")
    bucket.observe({"x-ratelimit-limit-tokens": "6000", "x-ratelimit-remaining-tokens": "1000"})
    wait = bucket.reserve(2500)
    check(abs(wait - 15.0) < 0.1, f"1,000 of 6,000 TPM left: a 2,500-token call waits {wait:.1f}s for the refill")
    second = bucket.reserve(500)
    check(abs(second - 20.0) < 0.1, f"the booking is visible to the next caller, who waits {second:.1f}s")
    bucket.observe({"x-ratelimit-limit-tokens": "6000", "x-ratelimit-remaining-tokens": "6000"})
    check(bucket.reserve(2500) == 0.0, "a full budget on the next response clears the wait")


def test_recall_timeout_falls_back() -> None:
    print("\n[4] Hindsight recall over the 1.5 s budget degrades to local memory")

    async def run() -> None:
        cfg = offline_settings()
        mock = MockHindsight(cfg.hindsight_api_key, slow_recall_s=cfg.hindsight_timeout_s + 1.0)
        memory = offline_memory(cfg, mock)
        query, tags = "30XA E-412 inverter overcurrent", ["model:carrier-30xa", "error:e-412"]
        start = time.perf_counter()
        outcome = await memory.recall_context(cfg.hindsight_bank_id, query, tags, label="fleet history")
        elapsed = time.perf_counter() - start
        check(outcome.status == "timeout" and outcome.source == "local_fallback", f"status={outcome.status}, source={outcome.source}")
        check(elapsed < cfg.hindsight_timeout_s + 0.5, f"pipeline unblocked after {elapsed:.2f}s (budget {cfg.hindsight_timeout_s}s)")
        check(any("Tech_Dave" in h.text for h in outcome.hits), "fallback still surfaced Tech_Dave's E-412 finding")
        await until(lambda: any("late Hindsight result cached" in e["summary"] for e in memory.events.list(op="recall")),
                    "the timed-out recall to finish", 5)
        again = await memory.recall_context(cfg.hindsight_bank_id, query, tags, label="fleet history")
        check(again.status == "cached" and again.source == "hindsight",
              "the timed-out call finished in the background and served the next recall from cache")
        await memory.aclose()

    asyncio.run(run())


def test_slow_retain_lands() -> None:
    print("\n[5] A retain slower than the recall budget lands in the background instead of being cut off")

    async def run() -> None:
        cfg = offline_settings()
        latency = cfg.hindsight_timeout_s + 1.0  # slower than the recall budget, well inside the retain budget
        mock = MockHindsight(cfg.hindsight_api_key, retain_latency_s=latency)
        runtime = Path(tempfile.mkdtemp())
        memory = offline_memory(cfg, mock, runtime)
        record = new_record("tkt-offline-slow-diagnosis")

        start = time.perf_counter()
        outcome = await memory.retain_records(cfg.hindsight_bank_id, [record], wait_s=0.2)
        waited = time.perf_counter() - start
        check(outcome.status == "sending" and outcome.source == "hindsight" and waited < 1.0,
              f"caller released after {waited:.1f}s with '{outcome.status}'; delivery continues in the background")
        check(memory.events.list(op="retain")[0]["status"] == "sending", "inspector shows the write as 'sending'")
        check(await pending_after_restart(cfg, mock, runtime) == 1,
              "journaled before sending: a restart mid-delivery would find it in the outbox")
        sync = await memory.sync_outbox([cfg.hindsight_bank_id])
        check(sync["pushed"] == 0 and sync["pending"] == 0 and retain_attempts(mock) == 1,
              "while in flight it is not outbox work, so a sync does not send it twice")

        await until(lambda: memory.events.list(op="retain")[0]["status"] != "sending", "the delivery to finish", latency + 5)
        events = memory.events.list(op="retain")
        check(len(events) == 1 and events[0]["status"] == "queued" and events[0]["source"] == "hindsight",
              f"the same inspector event moved from 'sending' to 'queued': {events[0]['summary']}")
        check(events[0]["latency_ms"] >= cfg.hindsight_timeout_s * 1000,
              f"accepted after {events[0]['latency_ms']} ms, past the {cfg.hindsight_timeout_s}s recall budget, "
              f"inside the {cfg.hindsight_retain_timeout_s:.0f}s retain budget")
        check([item["document_id"] for item in mock.retained] == [record.document_id], "Hindsight holds exactly one copy")
        check(await pending_after_restart(cfg, mock, runtime) == 0, "the ack is on disk, so a restart will not re-send it")
        await memory.aclose()

    asyncio.run(run())


def test_retain_outage() -> None:
    print("\n[6] Hindsight outage: the diagnosis still answers; the retain is retried, parked in the outbox and synced later")

    async def run() -> None:
        app, _, mock = build_offline_app()
        async with running(app) as client:
            mock.fail_retains = True
            resp = await client.post("/api/diagnose", json={"query": QUERY_B, "technician_id": "Tech_Alex", "mode": "copilot"})
            cop = resp.json()["copilot"]
            retain = cop["memory"]["retain"]
            check(resp.status_code == 200 and cop["ticket"] and "Tech_Dave" in cop["answer"] and not cop["warnings"],
                  "the technician still gets the full answer and a work order")
            check(retain["status"] == "deferred" and retain["source"] == "local_fallback" and "503" in (retain["error"] or ""),
                  f"retain deferred: {(retain['error'] or '')[:48]}…")
            event = (await client.get(f"/api/memory/events?op=retain&run_id={cop['run_id']}")).json()[0]
            # Without Retry-After the backoff alone would take at least 0.75 s + 1.5 s.
            check(retain_attempts(mock) == 1 + RETAIN_RETRIES and event["latency_ms"] < 1000,
                  f"{1 + RETAIN_RETRIES} attempts in {event['latency_ms']} ms, backing off per Hindsight's Retry-After")
            check(event["status"] == "deferred" and event["source"] == "local_fallback", f"inspector: {event['summary']}")
            check((await client.get("/api/memory/outbox")).json()["pending"] == 1, "the record waits in the outbox")

            sync = (await client.post("/api/memory/outbox/sync")).json()
            check(sync["pushed"] == 0 and sync["failed"] == 1 and sync["pending"] == 1, "a sync during the outage keeps it queued")
            mock.fail_retains = False
            sync = (await client.post("/api/memory/outbox/sync")).json()
            check(sync["pushed"] == 1 and sync["pending"] == 0, "once Hindsight is back, the outbox sync delivers it")
            check(mock.retained[-1]["document_id"] == retain["document_ids"][0], "Hindsight now holds the diagnosis record")

        # A rejected write is not an outage: retrying cannot help, so it goes straight to the outbox.
        cfg = offline_settings()
        wrong_key = MockHindsight("some-other-key")
        memory = offline_memory(cfg, wrong_key)
        rejected = await memory.retain_records(cfg.hindsight_bank_id, [new_record("tkt-offline-rejected-diagnosis")])
        await memory.aclose()
        check(rejected.status == "deferred" and "401" in (rejected.error or "") and retain_attempts(wrong_key) == 1,
              "a rejected key (401) is not retried")

    asyncio.run(run())


def test_end_to_end_learning_loop() -> None:
    print("\n[7] End-to-end: Tech_Dave's discovery helps Tech_Alex, whose confirmation then helps Tech_Sarah")

    async def run() -> None:
        app, services, mock = build_offline_app()
        async with running(app) as client:
            health = (await client.get("/api/health")).json()
            check(health["hindsight"]["reachable"] and health["llm"]["available"], "health: Hindsight reachable, LLM available")

            resp = await client.post("/api/diagnose", json={"query": QUERY_B, "technician_id": "Tech_Alex", "mode": "compare"})
            check(resp.status_code == 200, "POST /api/diagnose (compare) → 200")
            result = resp.json()
            base, cop = result["baseline"], result["copilot"]
            offered = {r["kind"]: r["tools"] for r in services.llm.requests}
            check(offered == {"baseline": ["fetch_unit_telemetry"], "copilot": ["fetch_unit_telemetry", "verify_fix_outcome"],
                              "work order": ["log_repair_ticket"]},
                  "one LLM, fair split: memory tools only for the copilot, the ticket tool only in the forced work-order call")

            check(base["memory"] is None and base["ticket"] is None and "Tech_Dave" not in base["answer"]
                  and "Replace inverter drive board" in base["answer"], "baseline: no memory, recommends the OEM board swap")
            memory = cop["memory"]
            check(memory["source"] == "hindsight" and memory["reflection"]["source"] == "hindsight",
                  f"copilot: recall and reflection served by Hindsight (reflect trigger: {memory['reflection']['trigger']})")
            delta = memory["delta"]
            best = delta["field_fixes"][0]
            check(delta["has_delta"] and best["discovered_by"] == "Tech_Dave", f"memory delta: {delta['headline'][:110]}…")
            answer = cop["answer"]
            check(f"held {best['held']} of {best['attempts']}" in answer and "(Tech_Dave, 2026-09-03, CHL-0417)" in answer,
                  f"answer quotes the verified hold count ({best['held']} of {best['attempts']}) and cites Tech_Dave's finding")
            check(answer.startswith("### Likely root cause") and "\u2011" not in answer, "answer markdown normalised before display")
            citations = cop["citations"]
            check(citations["total"] >= 2 and citations["verified"] == citations["total"],
                  f"all {citations['total']} citations verified against the ledger")

            tools = cop["tool_calls"]
            check([t["stage"] for t in tools] == ["prefetch", "diagnosis", "diagnosis", "diagnosis", "work order"],
                  "telemetry prefetched, three tool rounds, then the work order: " + " → ".join(t["name"] for t in tools))
            repaired = [t for t in tools if t["repaired"]]
            check(len(repaired) == 4 and not tools[0]["repaired"],
                  "every model-issued call needed repair: " + "; ".join(t["repair_notes"][-1] for t in repaired))
            kinds = [r["kind"] for r in cop["llm"]["recoveries"]]
            check(kinds == ["tool_use_failed", "text_tool_call"] and len(cop["llm"]["waits"]) == 1,
                  f"recovered from Groq faults: {kinds} (after one 429 backoff)")

            ticket = cop["ticket"]
            check(ticket["created_via"] == "agent_tool" and ticket["action_category"] == "field",
                  f"work order {ticket['id']} filed by the forced log_repair_ticket call")
            check(ticket["canonical_action"] == best["action"], "paraphrased fix mapped onto the ledger action")
            check(ticket["error_code"] == "E-412" and ticket["parts"] == ["J4 ferrule kit", "strain-relief bracket"],
                  "ticket arguments normalised (E412 → E-412, parts string → list)")
            retain = memory["retain"]
            check(retain["status"] == "queued" and retain["source"] == "hindsight",
                  f"diagnosis retained to Hindsight after the answer ({retain['operation_id']})")

            outcome = {"outcome_held": True, "notes": "J4 terminals loose again; re-terminated.", "technician_id": "Tech_Alex"}
            resp = await client.post(f"/api/tickets/{ticket['id']}/outcome", json=outcome)
            confirmed = resp.json()
            check(resp.status_code == 200 and confirmed["retain"]["status"] == "queued" and confirmed["ticket"]["status"] == "resolved",
                  "outcome confirmation retained; ticket resolved")
            check("HELD" in mock.retained[-1]["content"], "retained outcome record says the fix HELD")

            resp = await client.post("/api/diagnose", json={"query": FOLLOW_UP, "technician_id": "Tech_Sarah", "mode": "copilot"})
            later = resp.json()["copilot"]["memory"]["delta"]["field_fixes"][0]
            check((later["held"], later["attempts"]) == (best["held"] + 1, best["attempts"] + 1),
                  f"next session for Tech_Sarah sees the harness fix held {later['held']} of {later['attempts']} "
                  f"(was {best['held']} of {best['attempts']})")

            events = (await client.get("/api/memory/events?limit=400")).json()
            ops = {e["op"] for e in events}
            check({"recall", "retain", "reflect", "bank"} <= ops, f"memory inspector log has {len(events)} events across {sorted(ops)}")
            retains = [e for e in events if e["op"] == "retain"]
            check(len(retains) == 3 and {e["status"] for e in retains} == {"queued"},
                  "one inspector event per retain (2 diagnoses, 1 outcome), each settled as 'queued'")
            check(not any(secret in json.dumps(events).lower() for secret in ("authorization", "test-key")),
                  "no credentials in inspector payloads")
            health = (await client.get("/api/health")).json()
            check(health["outbox"]["pending"] == 0 and health["llm"]["rate"][services.llm.model]["limit_tpm"] == 250000,
                  "health: outbox empty; Groq's token budget tracked from response headers")

    asyncio.run(run())


def test_stream_reports_retain() -> None:
    print("\n[8] Streaming: the answer and work order arrive first; the retain follows as 'sending', then 'queued'")

    async def run() -> None:
        app, _, _ = build_offline_app()
        async with running(app) as client:
            resp = await client.post("/api/diagnose/stream", json={"query": QUERY_B, "technician_id": "Tech_Alex", "mode": "copilot"})
            check(resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream"),
                  "POST /api/diagnose/stream → text/event-stream")
            events = sse_events(resp.text)
        names = [name for name, _ in events]
        first = {name: names.index(name) for name in ("answer", "ticket", "retain")}
        check(first["answer"] < first["ticket"] < first["retain"] and names[-1] == "done",
              "event order: … " + " → ".join(names[first["answer"]:]))
        retains = [data for name, data in events if name == "retain"]
        check([r["status"] for r in retains] == ["sending", "queued"] and {r["source"] for r in retains} == {"hindsight"},
              "retain reported as 'sending' while in flight, then 'queued' once Hindsight acknowledged it")
        check("llm_wait" in names, "the 429 backoff was surfaced to the UI as an llm_wait event")
        run_view = events[-1][1]["run"]
        check(run_view["memory"]["retain"]["status"] == "queued" and run_view["ticket"]["id"] == events[first["ticket"]][1]["ticket"]["id"],
              "the final run payload agrees with the streamed events")

    asyncio.run(run())


def test_field_notes_briefing() -> None:
    print("\n[12] Field notes: one technician's site knowledge briefs the next visit")
    screened = screen_note("Gate code is 4471#. Call +1 (555) 201-4478. Torque J4 to 2.5 N·m; pin 3; 0.35-0.45 mm.")
    check("4471" not in screened.text and "201-4478" not in screened.text,
          f"access codes and phone numbers are redacted: {screened.redactions}")
    check(all(x in screened.text for x in ("2.5 N·m", "pin 3", "0.35-0.45 mm")), "sizes, torques and pin numbers survive")

    async def run() -> None:
        app, services, mock = build_offline_app()
        async with running(app) as client:
            async def briefing(unit: str) -> dict[str, Any]:
                return (await client.get("/api/briefing", params={"unit_id": unit})).json()

            check((await briefing("CHL-0417"))["items"] == [], "CHL-0417 at Riverside Medical starts with an empty briefing")

            resp = await client.post("/api/notes", json={
                "text": "Tower B roof needs a facilities escort after 6pm. Gate code is 4471#.",
                "technician_id": "Tech_Alex", "unit_id": "CHL-0417", "scope": "site", "kind": "site_rule"})
            saved = resp.json()
            check(resp.status_code == 200 and saved["retain"]["status"] == "queued" and saved["note"]["scope"] == "site",
                  "Tech_Alex's site rule retained to Hindsight (site-wide)")
            check(saved["redactions"] and "4471" not in json.dumps(mock.retained),
                  "the gate code never reached shared memory")
            quirk = await client.post("/api/notes", json={
                "text": "VFD cabinet door hinge is seized; bring a 10 mm socket.",
                "technician_id": "Tech_Alex", "unit_id": "CHL-0417"})
            check(quirk.json()["note"]["kind"] == "machine_quirk" and quirk.json()["note"]["scope"] == "unit",
                  "kind inferred as an equipment quirk, pinned to CHL-0417")

            mine = await briefing("CHL-0417")
            check([i["kind"] for i in mine["items"]] == ["site_rule", "machine_quirk"]
                  and {i["technician"] for i in mine["items"]} == {"Tech_Alex"} and mine["source"] == "hindsight",
                  "CHL-0417 briefing now holds both, credited to Tech_Alex, recalled from Hindsight")
            other = await briefing("ELV-0302")  # another unit at Riverside Medical
            check(["escort" in i["text"] for i in other["items"]] == [True],
                  "the site rule reaches every unit at Riverside; the chiller quirk stays with CHL-0417")
            check(all("Harbor" not in i["text"] and "Coastal" not in i["text"] for i in mine["items"]),
                  "notes from other sites do not leak in")
            mesa = await briefing("INV-3112")
            check([i["kind"] for i in mesa["items"]] == ["hazard", "machine_quirk"],
                  f"seeded knowledge: Mesa Ridge briefing leads with the heat hazard, then the INV-3112 quirk")

            resp = await client.post("/api/diagnose/stream", json={"query": QUERY_B.replace("Tech_Alex", "Tech_Sarah"),
                                                                    "technician_id": "Tech_Sarah", "mode": "copilot"})
            events = sse_events(resp.text)
            brief = next(data for name, data in events if name == "briefing")
            check(brief["count"] == 2 and brief["source"] == "hindsight",
                  "Tech_Sarah's diagnosis on CHL-0417 streams the 2-note briefing before reasoning")
            contexts = [r["context"] for r in services.llm.requests if r["kind"] == "copilot"]
            check(any("FIELD NOTES FROM TECHNICIANS" in c and "escort" in c and "Tech_Alex" in c for c in contexts),
                  "the notes, credited to Tech_Alex, are in the Copilot's context")
            check(events[-1][1]["run"]["memory"]["briefing"]["items"][0]["kind"] == "site_rule",
                  "the run payload carries the briefing for the UI")

            bad = await client.post("/api/notes", json={"text": "code 4471", "technician_id": "Tech_Alex", "unit_id": "CHL-0417"})
            check(bad.status_code == 422, "a note that is only a code is rejected, not stored")
            unknown = await client.post("/api/notes", json={"text": "Roof hatch sticks in the cold.", "technician_id": "Tech_Alex", "unit_id": "XYZ-0001"})
            check(unknown.status_code == 422, "unknown units are rejected")

    asyncio.run(run())


def test_demo_reset() -> None:
    print("\n[13] Demo reset: a rehearsal is undone, seeded memory is untouched")

    async def run() -> None:
        app, _, mock = build_offline_app()
        async with running(app) as client:
            cp = (await client.post("/api/demo/checkpoint", json={"label": "clean"})).json()
            check(cp["label"] == "clean" and cp["records"] == 0, "checkpoint saved before the rehearsal")
            seeded = {r.record_id for r in mock.store.all_records()}

            # A full rehearsal: a note, a diagnosis with its work order, an outcome, an approved bulletin.
            note = (await client.post("/api/notes", json={"text": "Tower B roof needs a facilities escort after 6pm.",
                                                          "technician_id": "Tech_Alex", "unit_id": "CHL-0417", "scope": "site"})).json()
            run_ = (await client.post("/api/diagnose", json={"query": QUERY_B, "technician_id": "Tech_Alex", "mode": "copilot"})).json()
            ticket = run_["copilot"]["ticket"]["id"]
            await client.post(f"/api/tickets/{ticket}/outcome", json={"outcome_held": True})
            bulletin = (await client.post("/api/bulletins/draft", json={"model": "carrier-30xa", "error_code": "E-412"})).json()
            await client.post(f"/api/bulletins/{bulletin['id']}/approve", json={"approver": "Service Manager"})
            await until(lambda: not app.state.services.memory._inflight_docs, "retains to land", 20)
            check(len((await client.get("/api/briefing", params={"unit_id": "CHL-0417"})).json()["items"]) == 1,
                  "rehearsal left a note on CHL-0417")

            status = (await client.get("/api/demo")).json()
            c = status["preview"]["counts"]
            check((c["notes"], c["sessions"], c["outcomes"], c["bulletins"], c["tickets"]) == (1, 1, 1, 1, 1),
                  f"preview lists exactly the rehearsal: {c}")
            refused = await client.post("/api/demo/reset", json={})
            check(refused.status_code == 422, "a reset without explicit confirmation is refused")

            result = (await client.post("/api/demo/reset", json={"confirm": True})).json()
            gone = set(result["deleted_documents"])
            check({note["note"]["id"], f"{ticket.lower()}-diagnosis", f"{ticket.lower()}-outcome", bulletin["id"].lower()} <= gone,
                  f"deleted from Hindsight: {len(gone)} documents")
            check(result["deleted_directives"] == [f"bulletin-{bulletin['id'].lower()}"]
                  and not any(d["name"].startswith("bulletin-") for d in mock.directives),
                  "the bulletin's scoped directive is removed; base directives stay")
            check(not (gone & seeded) and seeded <= {r.record_id for r in mock.store.all_records()},
                  "no seeded document was touched")

            check((await client.get("/api/briefing", params={"unit_id": "CHL-0417"})).json()["items"] == [],
                  "CHL-0417 is back to an empty briefing")
            check((await client.get("/api/tickets")).json() == [], "local tickets restored to the checkpoint")
            b = (await client.get("/api/bulletins")).json()
            check(b["bulletins"] == [] and all(c["bulletin"] is None for c in b["candidates"]),
                  "bulletins restored: E-412 can be drafted and approved again")
            check((await client.get("/api/memory/outbox")).json()["pending"] == 0, "nothing left waiting in the outbox")
            again = (await client.get("/api/demo")).json()["preview"]
            check(again["nothing_to_do"], "a second reset has nothing to do")

    async def disabled() -> None:
        cfg = offline_settings(demo_tools=False)
        services = build_services(cfg, llm=ScriptedGroq(cfg), runtime_dir=Path(tempfile.mkdtemp()),
                                  hindsight_transport=httpx.MockTransport(MockHindsight(cfg.hindsight_api_key).handler))
        async with running(create_app(cfg, services=services)) as client:
            check((await client.post("/api/demo/reset", json={"confirm": True})).status_code == 404,
                  "demo tools are off unless enabled (off by default outside development)")

    asyncio.run(run())
    asyncio.run(disabled())


def test_bulletin_publish() -> None:
    print("\n[9] Service bulletin: drafted by structured reflect, approved into memory and a scoped directive")

    async def run() -> None:
        app, _, mock = build_offline_app()
        async with running(app) as client:
            candidates = (await client.get("/api/bulletins")).json()["candidates"]
            evidence = next(c["evidence"] for c in candidates if (c["model_key"], c["error_code"]) == ("carrier-30xa", "E-412"))
            fix, oem = evidence["field_fixes"][0], evidence["oem_step"]
            check((fix["held"], fix["attempts"], oem["held"], oem["attempts"]) == (7, 7, 1, 2),
                  f"candidate 30XA E-412 from the ledger: field fix held {fix['held']}/{fix['attempts']}, "
                  f"OEM step {oem['held']}/{oem['attempts']}")
            before = {d["name"] for d in (await client.get("/api/memory/directives")).json()["items"]}

            draft = (await client.post("/api/bulletins/draft", json={"model": "carrier-30xa", "error_code": "e-412"})).json()
            check(draft["generator"] == "hindsight_reflect" and draft["status"] == "draft",
                  f"{draft['id']} drafted by Hindsight structured reflect: {draft['content']['title']}")
            approved = (await client.post(f"/api/bulletins/{draft['id']}/approve", json={"approver": "Service Manager"})).json()
            publish = approved["publish"]
            check(approved["status"] == "approved" and publish["memory"].startswith("queued") and publish["directive"] == "created",
                  f"approved: bulletin retained ({publish['memory']}), directive {publish['directive']}")
            check(mock.retained[-1]["document_id"] == draft["id"].lower() and "record:bulletin" in mock.retained[-1]["tags"],
                  "the bulletin itself is now a memory")
            name = f"bulletin-{draft['id'].lower()}"
            directive = next(d for d in mock.directives if d["name"] == name)
            check(directive["tags"] == ["model:carrier-30xa", "error:e-412"]
                  and "First corrective action: Re-terminate the J4 harness" in directive["content"],
                  "directive scoped to 30XA E-412; its first corrective action skips the lockout step")
            after = {d["name"] for d in (await client.get("/api/memory/directives")).json()["items"]}
            check(name not in before and name in after, "directive listing refreshed on approval, not served from its 60 s cache")
            markdown = await client.get(f"/api/bulletins/{draft['id']}/markdown")
            check(markdown.status_code == 200 and "| 7/7 |" in markdown.text and "approved by Service Manager" in markdown.text,
                  "markdown export carries the ledger evidence and the approval")

    asyncio.run(run())


def test_metrics_learning_curve() -> None:
    print("\n[10] Fleet learning curve")

    async def run() -> dict[str, Any]:
        app, _, _ = build_offline_app()
        async with running(app) as client:
            return (await client.get("/api/metrics/fleet")).json()

    m = asyncio.run(run())
    rates = [round(w["ftf_rate"] * 100) for w in m["weeks"]]
    check(rates == [61, 72, 83, 94], f"first-time-fix by week: {rates}")
    density = [w["memory_records"] for w in m["weeks"]]
    check(density == sorted(density), f"memory density grows: {density}")
    alex = next(t for t in m["technicians"] if t["technician"] == "Tech_Alex")
    check(alex["ftf_weeks_3_4"] > alex["ftf_weeks_1_2"], f"junior tech FTF {alex['ftf_weeks_1_2']:.0%} → {alex['ftf_weeks_3_4']:.0%}")


def test_fully_degraded() -> None:
    print("\n[11] No keys at all: still answers from manual + local memory, and keeps the session for later")

    async def run() -> None:
        cfg = offline_settings(hindsight_api_key=None, groq_api_key=None)
        app = create_app(cfg, services=build_services(cfg, runtime_dir=Path(tempfile.mkdtemp())))
        async with running(app) as client:
            cop = (await client.post("/api/diagnose", json={"query": QUERY_B, "mode": "copilot"})).json()["copilot"]
            outbox = (await client.get("/api/memory/outbox")).json()
        check(cop["memory"]["source"] == "local_fallback", "recall used the local fallback store")
        check("LLM unavailable" in cop["answer"] and "Tech_Dave" in cop["answer"], "deterministic brief still carries the memory delta")
        check(cop["ticket"]["created_via"] == "auto", f"work order {cop['ticket']['id']} still filed")
        check(cop["memory"]["retain"]["status"] == "skipped" and outbox["pending"] == 1,
              "session journaled to the outbox; it syncs once a Hindsight key is configured")

    asyncio.run(run())


# ======================================================================= live
async def live() -> int:
    if not (settings.groq_enabled and settings.hindsight_enabled):
        print("--live needs GROQ_API_KEY and HINDSIGHT_API_KEY in .env")
        return 2
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://copilot", timeout=180) as client:
            print(json.dumps((await client.get("/api/health")).json(), indent=2))
            result = (await client.post("/api/diagnose", json={"query": QUERY_B, "technician_id": "Tech_Alex", "mode": "compare"})).json()
            for mode in ("baseline", "copilot"):
                run = result[mode]
                print(f"\n===== {mode.upper()} ({run['timings']}) =====\n{run['answer']}")
            cop = result["copilot"]
            print(f"\nMemory source: {cop['memory']['source']} | delta: {cop['memory']['delta']['headline']}")
            print("Tools:", [(t["name"], t["repaired"]) for t in cop["tool_calls"]])
            print(f"Citations: {cop['citations']['verified']}/{cop['citations']['total']} verified")
            if cop["ticket"]:
                await client.post(f"/api/tickets/{cop['ticket']['id']}/outcome", json={"outcome_held": True})
            # Retains finish in the background; wait for them so the run reports how each one ended.
            deadline = time.monotonic() + (1 + RETAIN_RETRIES) * settings.hindsight_retain_timeout_s + 10
            while True:
                retains = (await client.get("/api/memory/events?op=retain")).json()
                if all(e["status"] != "sending" for e in retains) or time.monotonic() > deadline:
                    break
                await asyncio.sleep(1)
            for e in reversed(retains):
                print(f"Retain {e['status']}: {e['summary']}" + (f" ({e['error']})" if e["error"] else ""))
            print("Outbox:", (await client.get("/api/memory/outbox")).json())
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use real Groq and Hindsight from .env")
    args = parser.parse_args()
    if args.live:
        sys.exit(asyncio.run(live()))
    # The scripted faults make the app log expected warnings; the checks below report what matters.
    logging.getLogger().setLevel(logging.ERROR)
    tests = [
        test_json_repair, test_citations, test_token_bucket, test_recall_timeout_falls_back, test_slow_retain_lands,
        test_retain_outage, test_end_to_end_learning_loop, test_stream_reports_retain, test_bulletin_publish,
        test_metrics_learning_curve, test_fully_degraded, test_field_notes_briefing, test_demo_reset,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # report and keep going so one failure doesn't hide the rest
            failed += 1
            print(f"  ✗ {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} workflow checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
