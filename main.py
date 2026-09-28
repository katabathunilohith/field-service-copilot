"""Field Service Copilot API server.

    python main.py            # serve on 127.0.0.1:$PORT
    python main.py --reload   # auto-reload on code changes
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.groq_client import GroqClient
from agent.orchestrator import DiagnoseRequest, DiagnosticOrchestrator
from config import ROOT_DIR, RUNTIME_DIR, Settings, settings as default_settings
from fleet.catalog import get_catalog
from fleet.metrics import fleet_metrics
from fleet.tickets import TicketLedger
from memory.bank_schemas import all_bank_ids, load_seed, records_from_seed
from memory.event_log import EventLog
from memory.fallback_store import LocalMemoryStore
from memory.hindsight_wrapper import HindsightMemory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("copilot.api")

UI_DIST = ROOT_DIR / "ui" / "dist"


# ------------------------------------------------------------------ schemas
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class DiagnoseBody(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    technician_id: str = Field(default="Tech_Alex", pattern=r"^Tech_[A-Za-z]{2,20}$")
    unit_id: str | None = Field(default=None, max_length=12)
    mode: Literal["baseline", "copilot", "compare"] = "compare"
    history: dict[Literal["baseline", "copilot"], list[ChatTurn]] = Field(default_factory=dict)


class OutcomeBody(BaseModel):
    outcome_held: bool
    notes: str = Field(default="", max_length=1000)
    technician_id: str | None = Field(default=None, pattern=r"^Tech_[A-Za-z]{2,20}$")


class ReflectBody(BaseModel):
    model: str
    error_code: str
    force: bool = False


# ------------------------------------------------------------------ services
@dataclass
class Services:
    cfg: Settings
    events: EventLog
    fallback: LocalMemoryStore
    memory: HindsightMemory
    llm: GroqClient
    tickets: TicketLedger
    orchestrator: DiagnosticOrchestrator
    seed: dict[str, Any]
    health_cache: tuple[float, dict[str, Any]] | None = None


def build_services(cfg: Settings, *, llm: GroqClient | None = None, runtime_dir=RUNTIME_DIR) -> Services:
    seed = load_seed()
    events = EventLog()
    fallback = LocalMemoryStore(records_from_seed(seed), runtime_dir / "memory_journal.jsonl")
    memory = HindsightMemory(cfg, event_log=events, fallback=fallback)
    llm = llm or GroqClient(cfg)
    tickets = TicketLedger(runtime_dir / "tickets.jsonl")
    orchestrator = DiagnosticOrchestrator(llm=llm, memory=memory, catalog=get_catalog(), tickets=tickets)
    return Services(cfg, events, fallback, memory, llm, tickets, orchestrator, seed)


def create_app(cfg: Settings = default_settings, *, services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services or build_services(cfg)
        svc: Services = app.state.services
        log.info(
            "Field Service Copilot ready: hindsight=%s bank=%s groq=%s model=%s fallback_records=%d",
            "on" if cfg.hindsight_enabled else "OFF (local fallback)", cfg.hindsight_bank_id,
            "on" if svc.llm.available else "OFF", cfg.groq_model, svc.fallback.size,
        )
        yield
        await svc.memory.aclose()

    app = FastAPI(title="Field Service Copilot", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(cfg.cors_origins), allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )

    def svc(request: Request) -> Services:
        return request.app.state.services

    # -------------------------------------------------------------- routes
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        s = svc(request)
        now = time.time()
        if s.health_cache is None or s.health_cache[0] < now:
            probe = await s.memory.health(all_bank_ids()[0])
            s.health_cache = (now + 60, probe)
        return {
            "status": "ok",
            "config": s.cfg.public_summary(),
            "hindsight": s.health_cache[1],
            "llm": {"available": s.llm.available, "model": s.llm.model},
            "fallback_store_records": s.fallback.size,
            "banks": all_bank_ids(),
        }

    @app.get("/api/catalog")
    async def catalog() -> dict[str, Any]:
        return get_catalog().public()

    @app.post("/api/diagnose")
    async def diagnose(body: DiagnoseBody, request: Request) -> dict[str, Any]:
        req = DiagnoseRequest(
            query=body.query.strip(),
            technician_id=body.technician_id,
            unit_id=body.unit_id.upper() if body.unit_id else None,
            history={mode: [t.model_dump() for t in turns] for mode, turns in body.history.items()},
        )
        return await svc(request).orchestrator.diagnose(req, body.mode)

    @app.get("/api/tickets")
    async def tickets(request: Request, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return svc(request).tickets.list(limit)

    @app.post("/api/tickets/{ticket_id}/outcome")
    async def confirm_outcome(ticket_id: str, body: OutcomeBody, request: Request) -> dict[str, Any]:
        try:
            return await svc(request).orchestrator.confirm_outcome(
                ticket_id, outcome_held=body.outcome_held, notes=body.notes.strip(), technician_id=body.technician_id,
            )
        except KeyError:
            raise HTTPException(404, f"Unknown ticket {ticket_id}") from None

    @app.get("/api/memory/events")
    async def memory_events(
        request: Request,
        limit: int = Query(100, ge=1, le=400),
        op: Literal["recall", "retain", "reflect", "bank"] | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return svc(request).events.list(limit=limit, op=op, run_id=run_id)

    @app.post("/api/memory/reflect")
    async def reflect(body: ReflectBody, request: Request) -> dict[str, Any]:
        try:
            return await svc(request).orchestrator.reflect(body.model, body.error_code.upper(), force=body.force)
        except KeyError:
            raise HTTPException(404, f"Unknown model/error {body.model}/{body.error_code}") from None

    @app.get("/api/metrics/fleet")
    async def metrics(request: Request) -> dict[str, Any]:
        s = svc(request)
        live_records = s.fallback.size - sum(len(wo["visits"]) for wo in s.seed["work_orders"])
        return fleet_metrics(s.seed, get_catalog(), s.tickets.list(1000), max(0, live_records))

    # Serve the production UI build when present (npm run build in ui/).
    if UI_DIST.exists():
        app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = (UI_DIST / path).resolve()
            if path and candidate.is_file() and UI_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(UI_DIST / "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Field Service Copilot API")
    parser.add_argument("--reload", action="store_true", help="reload on code changes")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=default_settings.port, reload=args.reload)
