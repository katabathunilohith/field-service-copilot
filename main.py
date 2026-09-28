"""Field Service Copilot API server.

    python main.py            # serve on 127.0.0.1:$PORT
    python main.py --reload   # auto-reload on code changes
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.groq_client import GroqClient, LLMUnavailable
from agent.orchestrator import DiagnoseRequest, DiagnosticOrchestrator
from config import ROOT_DIR, RUNTIME_DIR, Settings, settings as default_settings
from fleet.bulletins import BulletinService, to_markdown
from fleet.catalog import get_catalog
from fleet.metrics import fleet_metrics
from fleet.tickets import TicketLedger
from memory.bank_schemas import all_bank_ids, bank_schema, load_seed, records_from_seed
from memory.event_log import EventLog
from memory.fallback_store import LocalMemoryStore
from memory.field_notes import FieldNotes, seed_notes
from memory.hindsight_wrapper import HindsightError, HindsightMemory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("copilot.api")

UI_DIST = ROOT_DIR / "ui" / "dist"
EVAL_RESULTS = ROOT_DIR / "eval" / "results" / "latest.json"
MAX_AUDIO_BYTES = 10 * 1024 * 1024
RATE_LIMITED_PATHS = ("/api/diagnose", "/api/transcribe", "/api/bulletins/draft")


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

    def to_request(self) -> DiagnoseRequest:
        return DiagnoseRequest(
            query=self.query.strip(),
            technician_id=self.technician_id,
            unit_id=self.unit_id.upper() if self.unit_id else None,
            history={mode: [t.model_dump() for t in turns] for mode, turns in self.history.items()},
        )


class PrefetchBody(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    technician_id: str | None = Field(default=None, pattern=r"^Tech_[A-Za-z]{2,20}$")
    unit_id: str | None = Field(default=None, max_length=12)


class OutcomeBody(BaseModel):
    outcome_held: bool
    notes: str = Field(default="", max_length=1000)
    technician_id: str | None = Field(default=None, pattern=r"^Tech_[A-Za-z]{2,20}$")


class ReflectBody(BaseModel):
    model: str
    error_code: str
    force: bool = False


class BulletinDraftBody(BaseModel):
    model: str
    error_code: str


class BulletinApproveBody(BaseModel):
    approver: str = Field(default="Service Manager", min_length=2, max_length=60)


class NoteBody(BaseModel):
    text: str = Field(min_length=8, max_length=500)
    technician_id: str = Field(pattern=r"^Tech_[A-Za-z]{2,20}$")
    unit_id: str = Field(max_length=12)
    scope: Literal["unit", "site"] = "unit"
    kind: Literal["site_rule", "hazard", "machine_quirk"] | None = None


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
    bulletins: BulletinService
    seed: dict[str, Any]
    health_cache: tuple[float, dict[str, Any]] | None = None
    directives_cache: tuple[float, dict[str, Any]] | None = None
    runs: set[asyncio.Task] = field(default_factory=set)
    notes: FieldNotes | None = None


def build_services(
    cfg: Settings, *, llm: GroqClient | None = None, runtime_dir=RUNTIME_DIR, hindsight_transport=None,
) -> Services:
    """Wire the app. `hindsight_transport` lets tests point the real wrapper at a mock API."""
    seed = load_seed()
    events = EventLog()
    catalog = get_catalog()
    fallback = LocalMemoryStore(records_from_seed(seed) + seed_notes(catalog), runtime_dir / "memory_journal.jsonl")
    memory = HindsightMemory(cfg, event_log=events, fallback=fallback, acks_path=runtime_dir / "retain_acks.jsonl",
                             transport=hindsight_transport)
    llm = llm or GroqClient(cfg)
    tickets = TicketLedger(runtime_dir / "tickets.jsonl")
    notes = FieldNotes(memory, catalog)
    orchestrator = DiagnosticOrchestrator(llm=llm, memory=memory, catalog=catalog, tickets=tickets, notes=notes)
    bulletins = BulletinService(runtime_dir / "bulletins.jsonl", catalog, memory)
    return Services(cfg, events, fallback, memory, llm, tickets, orchestrator, bulletins, seed, notes=notes)


class RateLimiter:
    """Sliding one-minute window per client for the endpoints that spend LLM/memory quota."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client: str) -> float | None:
        """None if allowed, else seconds until the next slot frees up."""
        if self.per_minute <= 0:
            return None
        now = time.monotonic()
        window = self._hits[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return 60 - (now - window[0])
        window.append(now)
        return None


def _whisper_prompt() -> str:
    """Bias speech recognition toward fleet vocabulary: unit IDs, codes, models, names."""
    catalog = get_catalog()
    codes = sorted({c["code"] for m in catalog.public()["models"] for c in m["error_codes"]})
    units = sorted(catalog.units)
    models = [m["name"] for m in catalog.models.values()]
    names = list(catalog.technicians)
    return ("Field service call. " + ", ".join(models) + ". Error codes: " + ", ".join(codes) + ". Units: "
            + ", ".join(units) + ". Technicians: " + ", ".join(names) + ". Lockout/tagout, VFD, GFDI, MC4, KDL16.")


def create_app(cfg: Settings = default_settings, *, services: Services | None = None) -> FastAPI:
    limiter = RateLimiter(cfg.rate_limit_per_min)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services or build_services(cfg)
        svc: Services = app.state.services
        log.info(
            "Field Service Copilot ready: hindsight=%s bank=%s groq=%s model=%s fallback_records=%d",
            "on" if cfg.hindsight_enabled else "OFF (local fallback)", cfg.hindsight_bank_id,
            "on" if svc.llm.available else "OFF", cfg.groq_model, svc.fallback.size,
        )
        for hint in cfg.setup_hints():
            log.warning("setup: %s", hint)
        if svc.memory.enabled:
            async def ensure_banks() -> None:
                for bank_id in all_bank_ids():
                    try:
                        await svc.memory.ensure_bank(bank_schema(bank_id))
                    except (TimeoutError, HindsightError) as exc:
                        log.warning("could not verify bank %s: %s", bank_id, exc)
            svc.runs.add(asyncio.create_task(ensure_banks()))
            svc.memory.start_background(bank_ids=all_bank_ids(), prewarm=svc.orchestrator.reflection_candidates())
        yield
        for task in list(svc.runs):
            task.cancel()
        await svc.memory.aclose()

    app = FastAPI(title="Field Service Copilot", version="1.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(cfg.cors_origins), allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Access-Token"],
    )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request_id = secrets.token_hex(6)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health" and request.method != "OPTIONS":
            if cfg.app_access_token:
                supplied = request.headers.get("x-access-token", "")
                if not hmac.compare_digest(supplied, cfg.app_access_token):
                    return JSONResponse({"detail": "Access token required"}, status_code=401,
                                        headers={"X-Request-ID": request_id})
            if request.method == "POST" and path.startswith(RATE_LIMITED_PATHS):
                client = request.client.host if request.client else "unknown"
                wait = limiter.check(client)
                if wait is not None:
                    return JSONResponse({"detail": f"Rate limit reached; retry in {wait:.0f}s"}, status_code=429,
                                        headers={"Retry-After": str(int(wait) + 1), "X-Request-ID": request_id})
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        if not path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'"
            )
        return response

    def svc(request: Request) -> Services:
        return request.app.state.services

    # -------------------------------------------------------------- status
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        s = svc(request)
        now = time.time()
        if s.health_cache is None or s.health_cache[0] < now:
            probe = await s.memory.health(all_bank_ids()[0])
            # Cache a healthy result for a minute, a failure only briefly so recovery shows quickly.
            s.health_cache = (now + (60 if probe.get("reachable") else 10), probe)
        return {
            "status": "ok",
            "config": s.cfg.public_summary(),
            "hindsight": s.health_cache[1],
            "llm": {"available": s.llm.available, "model": s.llm.model, "baseline_model": s.llm.baseline_model,
                    "rate": s.llm.rate_status()},
            "outbox": s.memory.outbox_status(),
            "fallback_store_records": s.fallback.size,
            "banks": all_bank_ids(),
        }

    @app.get("/api/catalog")
    async def catalog() -> dict[str, Any]:
        return get_catalog().public()

    # ----------------------------------------------------------- diagnosis
    @app.post("/api/diagnose")
    async def diagnose(body: DiagnoseBody, request: Request) -> dict[str, Any]:
        return await svc(request).orchestrator.diagnose(body.to_request(), body.mode)

    @app.post("/api/diagnose/stream")
    async def diagnose_stream(body: DiagnoseBody, request: Request) -> StreamingResponse:
        """Server-sent events for one pane: every pipeline stage as it happens, then the full run."""
        if body.mode == "compare":
            raise HTTPException(400, "Stream one mode at a time (baseline or copilot)")
        s = svc(request)
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            await queue.put((event, data))

        async def runner() -> None:
            try:
                run = await s.orchestrator.run(body.to_request(), body.mode, emit)  # type: ignore[arg-type]
                await queue.put(("done", {"run": run}))
            except Exception:  # surfaced to the client; details stay in the server log
                log.exception("diagnosis failed")
                await queue.put(("error", {"message": "The diagnosis failed. Check the server log for details."}))
            finally:
                await queue.put(None)

        # The run keeps going if the client disconnects, so its memory still gets retained.
        task = asyncio.create_task(runner())
        s.runs.add(task)
        task.add_done_callback(s.runs.discard)

        async def events():
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/memory/prefetch")
    async def prefetch(body: PrefetchBody, request: Request) -> dict[str, Any]:
        return svc(request).orchestrator.prefetch(
            body.query, unit_hint=body.unit_id.upper() if body.unit_id else None, technician_hint=body.technician_id,
        )

    @app.post("/api/transcribe")
    async def transcribe(request: Request, audio: UploadFile = File(...)) -> dict[str, Any]:
        s = svc(request)
        if not s.llm.available:
            raise HTTPException(503, "Voice input needs GROQ_API_KEY")
        content_type = (audio.content_type or "").split(";")[0]
        if not content_type.startswith(("audio/", "video/webm")):
            raise HTTPException(415, f"Unsupported media type {content_type or 'unknown'}")
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Recording too long (10 MB max)")
        if len(data) < 800:
            raise HTTPException(400, "Recording is empty")
        try:
            result = await s.llm.transcribe(data, audio.filename or "voice.webm", content_type, prompt=_whisper_prompt())
        except LLMUnavailable as exc:
            raise HTTPException(502, str(exc)) from None
        parsed = get_catalog().parse(result["text"])
        return {**result, "parsed": parsed.to_dict()}

    # ------------------------------------------------------------- tickets
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

    # -------------------------------------------------------------- memory
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

    @app.get("/api/memory/outbox")
    async def outbox(request: Request) -> dict[str, Any]:
        return svc(request).memory.outbox_status()

    @app.post("/api/memory/outbox/sync")
    async def outbox_sync(request: Request) -> dict[str, Any]:
        return await svc(request).memory.sync_outbox(all_bank_ids())

    @app.get("/api/memory/directives")
    async def directives(request: Request) -> dict[str, Any]:
        s = svc(request)
        defaults = [d.payload() for d in bank_schema(all_bank_ids()[0]).directives]
        if not s.memory.enabled:
            return {"source": "local", "items": defaults}
        if s.directives_cache and s.directives_cache[0] > time.time():
            return s.directives_cache[1]
        try:
            result = {"source": "hindsight", "items": await s.memory.list_directives(all_bank_ids()[0])}
        except (TimeoutError, HindsightError) as exc:
            return {"source": "local", "error": str(exc)[:200], "items": defaults}
        s.directives_cache = (time.time() + 60, result)
        return result

    # --------------------------------------------------------- field notes
    @app.post("/api/notes")
    async def add_note(body: NoteBody, request: Request) -> dict[str, Any]:
        """Retain a site rule, hazard or equipment quirk for every technician who visits next."""
        try:
            return await svc(request).notes.add(
                text=body.text, technician_id=body.technician_id, unit_id=body.unit_id.upper(),
                scope=body.scope, kind=body.kind,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/briefing")
    async def briefing(request: Request, unit_id: str = Query(..., max_length=12)) -> dict[str, Any]:
        """Pre-visit briefing: what the fleet has noted about this unit and its site."""
        try:
            return await svc(request).notes.briefing(unit_id.upper())
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    # ----------------------------------------------------------- bulletins
    @app.get("/api/bulletins")
    async def bulletins(request: Request) -> dict[str, Any]:
        s = svc(request)
        return {"candidates": s.bulletins.candidates(), "bulletins": s.bulletins.list()}

    @app.post("/api/bulletins/draft")
    async def draft_bulletin(body: BulletinDraftBody, request: Request) -> dict[str, Any]:
        try:
            return await svc(request).bulletins.draft(body.model, body.error_code.upper())
        except KeyError:
            raise HTTPException(404, f"Unknown model/error {body.model}/{body.error_code}") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/bulletins/{bulletin_id}/approve")
    async def approve_bulletin(bulletin_id: str, body: BulletinApproveBody, request: Request) -> dict[str, Any]:
        try:
            s = svc(request)
            s.directives_cache = None  # approval installs a new directive
            return await s.bulletins.approve(bulletin_id, body.approver.strip())
        except KeyError:
            raise HTTPException(404, f"Unknown bulletin {bulletin_id}") from None

    @app.get("/api/bulletins/{bulletin_id}/markdown")
    async def bulletin_markdown(bulletin_id: str, request: Request) -> PlainTextResponse:
        bulletin = svc(request).bulletins.get(bulletin_id)
        if bulletin is None:
            raise HTTPException(404, f"Unknown bulletin {bulletin_id}")
        return PlainTextResponse(to_markdown(bulletin), media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="{bulletin_id}.md"'})

    # ------------------------------------------------------------- metrics
    @app.get("/api/metrics/fleet")
    async def metrics(request: Request) -> dict[str, Any]:
        s = svc(request)
        live_records = len(s.fallback.runtime_records())
        return fleet_metrics(s.seed, get_catalog(), s.tickets.list(1000), live_records)

    @app.get("/api/eval/latest")
    async def eval_latest() -> dict[str, Any]:
        if not EVAL_RESULTS.exists():
            return {"available": False}
        return {"available": True, **json.loads(EVAL_RESULTS.read_text())}

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
