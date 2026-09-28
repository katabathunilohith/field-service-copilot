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
import sys
import tempfile
import time
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
from memory.fallback_store import LocalMemoryStore
from memory.hindsight_wrapper import HindsightMemory

QUERY_B = (
    "Tech_Alex here at Riverside Medical. Carrier AquaForce 30XA unit CHL-0417 is tripping E-412 again. "
    "The manual says replace the inverter board. Should I?"
)
FOLLOW_UP = "Tech_Sarah on Carrier AquaForce 30XA CHL-0631, E-412 overcurrent trips at start-up. Board or something else?"


# =============================================================== test doubles
class MockHindsight:
    """In-process Hindsight REST API backed by the seed data plus whatever gets retained.

    `retain_latency_s` makes writes slower than the 1.5 s recall budget, and
    `fail_retains` simulates an outage, which is exactly what the retain path must survive."""

    def __init__(self, api_key: str, *, slow_recall_s: float = 0.0, retain_latency_s: float = 0.0) -> None:
        self.api_key = api_key
        self.slow_recall_s = slow_recall_s
        self.retain_latency_s = retain_latency_s
        self.fail_retains = False
        self.store = LocalMemoryStore(records_from_seed(load_seed()), Path(tempfile.mkdtemp()) / "mock.jsonl")
        self.retained: list[dict[str, Any]] = []
        self.directives: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def _recall(self, body: dict[str, Any]) -> dict[str, Any]:
        strict = body.get("tags_match") in ("all_strict", "any_strict")
        hits = self.store.search(body["query"], body.get("tags"), limit=60 if strict else 12)
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
            if meta.get("record_type") in ("repair", "diagnosis", "outcome"):
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
                return httpx.Response(503, json={"detail": "temporarily unavailable"})
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
        if "/directives/" in path and method == "PATCH":
            return httpx.Response(200, json=body)
        if path.endswith("/config") or (method == "PUT" and path.count("/") == 4):
            return httpx.Response(200, json={"bank_id": bank})
        if path.endswith("/stats"):
            return httpx.Response(200, json={"total_nodes": self.store.size, "total_documents": self.store.size,
                                             "total_observations": 0, "pending_operations": 0})
        return httpx.Response(404, json={"detail": f"not found: {method} {path}"})
