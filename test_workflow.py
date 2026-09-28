"""End-to-end diagnosis workflow test. No browser required.

    python test_workflow.py            # offline: scripted LLM + mock Hindsight API (deterministic)
    python test_workflow.py --live     # real Groq + Hindsight using keys from .env
    pytest test_workflow.py            # same offline checks under pytest

The offline run drives the real FastAPI app, orchestrator, tools, Hindsight
wrapper and JSON-repair code. Only the two network boundaries are replaced:
Groq by a scripted model that deliberately emits malformed and text-embedded
tool calls, and Hindsight by an in-process mock that speaks the REST API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import openai

from agent.groq_client import GroqClient, repair_json
from config import Settings, settings
from main import build_services, create_app
from memory.bank_schemas import RepairRecord, load_seed, records_from_seed
from memory.event_log import EventLog
from memory.fallback_store import LocalMemoryStore
from memory.hindsight_wrapper import HindsightMemory

QUERY_B = (
    "Tech_Alex here at Riverside Medical. Carrier AquaForce 30XA unit CHL-0417 is tripping E-412 again. "
    "The manual says replace the inverter board. Should I?"
)
FOLLOW_UP = "Tech_Sarah on Carrier AquaForce 30XA CHL-0631, E-412 overcurrent trips at start-up. Board or something else?"


# =============================================================== test doubles
class MockHindsight:
    """Minimal Hindsight REST API backed by the seed data plus whatever gets retained."""

    def __init__(self, api_key: str, slow_recall_s: float = 0.0) -> None:
        self.api_key = api_key
        self.slow_recall_s = slow_recall_s
        self.store = LocalMemoryStore(records_from_seed(load_seed()), Path(tempfile.mkdtemp()) / "mock.jsonl")
        self.retained: list[dict[str, Any]] = []
        self.calls: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.content or b"{}")
        if path.endswith("/memories/recall"):
            if self.slow_recall_s:
                await asyncio.sleep(self.slow_recall_s)
            hits = self.store.search(body["query"], body.get("tags"), limit=60 if body.get("tags_match") == "all_strict" else 12)
            if body.get("tags_match") == "all_strict":
                hits = [h for h in hits if all(t in h.tags for t in body.get("tags", []))]
            return httpx.Response(200, json={"results": [
                {"id": f"fact-{h.id}", "text": h.text, "type": h.type, "tags": h.tags, "metadata": h.metadata,
                 "document_id": h.document_id, "occurred_start": h.occurred_at, "scores": {"final": h.score}}
                for h in hits
            ]})
        if path.endswith("/memories"):
            for item in body["items"]:
                meta = item["metadata"]
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
            return httpx.Response(200, json={"success": True, "bank_id": path.split("/")[4], "items_count": len(body["items"]),
                                             "async": body.get("async", False), "operation_id": f"op-{len(self.retained)}"})
        if path.endswith("/reflect"):
            return httpx.Response(200, json={
                "text": "Across the fleet, E-412 on the 30XA is usually a loose J4 harness (Tech_Dave, 2026-09-03), not the board.",
                "based_on": {"memories": [{"id": "fact-1", "text": "J4 harness finding", "type": "world"}]},
            })
        if path.endswith("/stats"):
            return httpx.Response(200, json={"total_nodes": self.store.size})
        return httpx.Response(404, json={"detail": "not found"})


class ScriptedGroq(GroqClient):
    """Stands in for Groq at the raw-API boundary, so parsing and repair run for real.

    Copilot script: one 429, one Groq `tool_use_failed`, then a malformed python-dict call with a
    mangled tool name, a tool call embedded in text, a proper ticket call, and a final answer."""

    def __init__(self, cfg: Settings) -> None:
        super().__init__(cfg, client=object())  # any non-None client marks the LLM available
        self.requests: list[dict[str, Any]] = []
        self._faults = ["rate_limit", "tool_use_failed"]

    async def _create(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        messages = kwargs["messages"]
        copilot = "Field Service Copilot" in messages[0]["content"]
        tool_results = [m for m in messages if m["role"] == "tool"]
        context = messages[1]["content"]
        unit = next((w.strip("|:,") for w in context.split() if w.startswith(("CHL-", "INV-", "ELV-"))), "CHL-0417")

        if copilot and self._faults:
            fault = self._faults.pop(0)
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            if fault == "rate_limit":
                raise openai.RateLimitError("rate limited", response=httpx.Response(429, headers={"retry-after": "0.2"}, request=request), body=None)
            raise openai.BadRequestError(
                "tool_use_failed", response=httpx.Response(400, request=request),
                body={"code": "tool_use_failed", "failed_generation": f'<function=fetch_unit_telemetry>{{"unit_id": "{unit}"}}</function>'},
            )

        def tool_call(name: str, args: str) -> dict[str, Any]:
            return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{len(tool_results)}", "type": "function", "function": {"name": name, "arguments": args}}]}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940}, "model": self.model}

        def text(content: str) -> dict[str, Any]:
            return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 220, "total_tokens": 1420}, "model": self.model}

        if not copilot:
            if not tool_results:
                return tool_call("fetch_unit_telemetry", json.dumps({"unit_id": unit}))
            return text("### Likely root cause\nFailed inverter drive board per OEM §7.4.\n### Recommended procedure\n"
                        "1. LOTO. 2. Check supply imbalance. 3. Megger windings. 4. Replace board 30XA-VFD-412B.\n"
                        "### Verify before you leave\nRun 30 min at 50% load.")

        n = len(tool_results)
        if n == 0:  # only reached once the injected faults are used up
            return tool_call("fetch_unit_telemetry", json.dumps({"unit_id": unit}))
        if n == 1:  # malformed python-dict arguments + a namespaced tool name
            return tool_call("functions.verify_fix_outcome<|channel|>commentary",
                             f"{{'model': 'carrier-30xa', 'error_code': 'E412', 'proposed_action': 're-terminate J4 harness', 'unit_id': '{unit}',}}")
        if n == 2:  # tool call written into content instead of tool_calls
            return text('<tool_call>{"name": "log_repair_ticket", "arguments": {"unit_id": "%s", "error_code": "E-412", '
                        '"diagnosis": "Loose J4 harness at VFD terminal block", "recommended_action": '
                        '"Inspect and re-terminate the J4 power harness at the VFD terminal block; add strain relief", '
                        '"action_category": "field", "priority": "high"}}</tool_call>' % unit)
        verify = json.loads(tool_results[1]["content"])
        fix = verify["matched_history"] or {}
        return text(
            "### Likely root cause\nLoose, vibration-chafed J4 harness at the VFD terminal block.\n"
            "### Recommended procedure\n1. LOTO and verify DC bus < 50 V.\n2. Inspect and re-terminate the J4 harness (2.5 N·m).\n"
            "3. Only if the harness is sound, follow OEM §7.4 and replace the board.\n"
            f"### Memory delta: field vs. manual\nThe harness fix held {fix.get('held')} of {fix.get('attempts')} times "
            f"({fix.get('first_confirmed_by')}, {fix.get('first_confirmed_on')}, {fix.get('first_unit')}).\n"
            "### Verify before you leave\nRun compressor A1 at 50% load for 30 minutes and log current."
        )


# ==================================================================== helpers
def offline_settings() -> Settings:
    return replace(Settings(), hindsight_api_key="test-key", hindsight_base_url="https://hindsight.mock",
                   groq_api_key="test-key", groq_max_retries=2)


def build_offline_app(slow_recall_s: float = 0.0):
    cfg = offline_settings()
    mock = MockHindsight(cfg.hindsight_api_key, slow_recall_s)
    runtime = Path(tempfile.mkdtemp())
    services = build_services(cfg, llm=ScriptedGroq(cfg), runtime_dir=runtime)
    services.memory._http = httpx.AsyncClient(  # route the real wrapper through the mock API
        base_url=cfg.hindsight_base_url, transport=httpx.MockTransport(mock.handler),
        headers={"Authorization": f"Bearer {cfg.hindsight_api_key}", "Content-Type": "application/json"},
    )
    return create_app(cfg, services=services), services, mock


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


def test_recall_timeout_falls_back() -> None:
    print("\n[2] Hindsight recall over the 1.5 s budget degrades to local memory")

    async def run() -> None:
        cfg = offline_settings()
        mock = MockHindsight(cfg.hindsight_api_key, slow_recall_s=3.0)
        memory = HindsightMemory(
            cfg, event_log=EventLog(),
            fallback=LocalMemoryStore(records_from_seed(load_seed()), Path(tempfile.mkdtemp()) / "j.jsonl"),
            transport=httpx.MockTransport(mock.handler),
        )
        start = time.perf_counter()
        outcome = await memory.recall_context(cfg.hindsight_bank_id, "30XA E-412 inverter overcurrent", ["model:carrier-30xa", "error:e-412"])
        elapsed = time.perf_counter() - start
        await memory.aclose()
        check(outcome.status == "timeout" and outcome.source == "local_fallback", f"status={outcome.status}, source={outcome.source}")
        check(elapsed < cfg.hindsight_timeout_s + 0.5, f"pipeline unblocked after {elapsed:.2f}s (budget {cfg.hindsight_timeout_s}s)")
        check(any("Tech_Dave" in h.text for h in outcome.hits), "fallback still surfaced Tech_Dave's E-412 finding")

    asyncio.run(run())


def test_end_to_end_learning_loop() -> None:
    print("\n[3] End-to-end: Tech_Dave's discovery helps Tech_Alex, whose confirmation then helps Tech_Sarah")

    async def run() -> None:
        app, services, mock = build_offline_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://copilot") as client:
                health = (await client.get("/api/health")).json()
                check(health["hindsight"]["reachable"] and health["llm"]["available"], "health: Hindsight reachable, LLM available")

                resp = await client.post("/api/diagnose", json={"query": QUERY_B, "technician_id": "Tech_Alex", "mode": "compare"})
                check(resp.status_code == 200, "POST /api/diagnose (compare) → 200")
                result = resp.json()
                base, cop = result["baseline"], result["copilot"]

                check(base["memory"] is None and "Tech_Dave" not in base["answer"], "baseline: no memory, recommends the OEM board swap")
                check(cop["memory"]["source"] == "hindsight", "copilot: recall served by Hindsight")
                delta = cop["memory"]["delta"]
                best = delta["field_fixes"][0]
                check(delta["has_delta"] and best["discovered_by"] == "Tech_Dave", f"memory delta: {delta['headline'][:110]}…")
                check(cop["memory"]["reflection"] is not None, f"reflect triggered ({cop['memory']['reflection']['trigger']})")
                check("Tech_Dave" in cop["answer"], "copilot answer cites Tech_Dave's field finding")

                names = [t["name"] for t in cop["tool_calls"]]
                check(names == ["fetch_unit_telemetry", "verify_fix_outcome", "log_repair_ticket"], f"tool loop: {' → '.join(names)}")
                repaired = [t for t in cop["tool_calls"] if t["repaired"]]
                check(len(repaired) == 3, "all 3 tool calls needed repair: " + "; ".join(n for t in repaired for n in t["repair_notes"][:2]))
                kinds = [r["kind"] for r in cop["llm"]["recoveries"]]
                check(kinds == ["tool_use_failed", "text_tool_call"], f"recovered from Groq faults: {kinds} (after one 429 backoff)")

                ticket = cop["ticket"]
                check(ticket and ticket["created_via"] == "agent_tool", f"ticket {ticket['id']} opened by the agent")
                check(ticket["canonical_action"].startswith("Re-terminate VFD J4"), "paraphrased fix mapped onto the known ledger action")
                check(cop["memory"]["retain"]["status"] == "queued", "post-run diagnosis retained to Hindsight (async)")

                before = best["held"]
                resp = await client.post(f"/api/tickets/{ticket['id']}/outcome",
                                          json={"outcome_held": True, "notes": "J4 terminals loose again; re-terminated.", "technician_id": "Tech_Alex"})
                confirmed = resp.json()
                check(resp.status_code == 200 and confirmed["retain"]["status"] == "queued", "outcome confirmation retained")
                check("HELD" in mock.retained[-1]["content"], "retained outcome record says the fix HELD")

                resp = await client.post("/api/diagnose", json={"query": FOLLOW_UP, "technician_id": "Tech_Sarah", "mode": "copilot"})
                later = resp.json()["copilot"]["memory"]["delta"]["field_fixes"][0]
                check(later["held"] == before + 1, f"next session for Tech_Sarah sees the harness fix held {later['held']}× (was {before}×)")
                check("Tech_Alex" in later["technicians"], "Tech_Alex is now a contributor to the fleet pattern")

                events = (await client.get("/api/memory/events?limit=100")).json()
                ops = {e["op"] for e in events}
                check({"recall", "retain", "reflect"} <= ops, f"memory inspector log has {len(events)} events across {sorted(ops)}")
                check(all("authorization" not in json.dumps(e).lower() for e in events), "no credentials in inspector payloads")

    asyncio.run(run())


def test_metrics_learning_curve() -> None:
    print("\n[4] Fleet learning curve")

    async def run() -> None:
        app, _, _ = build_offline_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://copilot") as client:
                m = (await client.get("/api/metrics/fleet")).json()
        rates = [round(w["ftf_rate"] * 100) for w in m["weeks"]]
        check(rates == [61, 72, 83, 94], f"first-time-fix by week: {rates}")
        density = [w["memory_records"] for w in m["weeks"]]
        check(density == sorted(density), f"memory density grows: {density}")
        alex = next(t for t in m["technicians"] if t["technician"] == "Tech_Alex")
        check(alex["ftf_weeks_3_4"] > alex["ftf_weeks_1_2"], f"junior tech FTF {alex['ftf_weeks_1_2']:.0%} → {alex['ftf_weeks_3_4']:.0%}")

    asyncio.run(run())


def test_fully_degraded() -> None:
    print("\n[5] No keys at all: still answers, from manual + local memory")

    async def run() -> None:
        cfg = replace(Settings(), hindsight_api_key=None, groq_api_key=None)
        services = build_services(cfg, runtime_dir=Path(tempfile.mkdtemp()))
        app = create_app(cfg, services=services)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://copilot") as client:
                cop = (await client.post("/api/diagnose", json={"query": QUERY_B, "mode": "copilot"})).json()["copilot"]
        check(cop["memory"]["source"] == "local_fallback", "recall used the local fallback store")
        check("LLM unavailable" in cop["answer"] and "Tech_Dave" in cop["answer"], "deterministic brief still carries the memory delta")

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
            if cop["ticket"]:
                confirm = (await client.post(f"/api/tickets/{cop['ticket']['id']}/outcome", json={"outcome_held": True})).json()
                print("Outcome retain:", confirm["retain"]["status"], confirm["retain"].get("operation_id"))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use real Groq and Hindsight from .env")
    args = parser.parse_args()
    if args.live:
        sys.exit(asyncio.run(live()))
    tests = [test_json_repair, test_recall_timeout_falls_back, test_end_to_end_learning_loop, test_metrics_learning_curve, test_fully_degraded]
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
