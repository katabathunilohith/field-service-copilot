"""Hindsight integration layer: retain, recall and reflect with graceful degradation.

Talks to the Hindsight REST API directly (the same endpoints `hindsight-client`
wraps) so every call gets a hard wall-clock budget and the exact request/response
can be shown in the Memory Inspector.

Degradation rules
-----------------
* recall  > HINDSIGHT_TIMEOUT_S (1.5 s) or error  -> local BM25 fallback store
* retain  > HINDSIGHT_TIMEOUT_S or error          -> journaled locally, retried in the background
* reflect > HINDSIGHT_REFLECT_TIMEOUT_S or error  -> deterministic synthesis from outcome stats
The agent pipeline never raises because memory is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import Settings, settings as default_settings
from memory.bank_schemas import BankSchema, RepairRecord, context_tags, error_tag, model_tag, now_iso
from memory.event_log import EventLog, MemoryEvent
from memory.fallback_store import LocalMemoryStore
from memory.insights import MemoryHit, render_local_synthesis, summarize_outcomes

log = logging.getLogger("copilot.memory")

API_PREFIX = "/v1/default/banks"
REFLECT_CACHE_TTL_S = 600


class HindsightError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class RecallOutcome:
    source: str
    status: str
    hits: list[MemoryHit]
    latency_ms: int
    event_id: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "event_id": self.event_id,
            "error": self.error,
            "hits": [h.to_dict() for h in self.hits],
        }


@dataclass
class RetainOutcome:
    status: str
    source: str
    event_id: int
    items: int
    operation_id: str | None = None
    error: str | None = None
    document_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ReflectOutcome:
    source: str
    status: str
    text: str
    based_on: list[dict[str, Any]]
    latency_ms: int
    event_id: int
    cached: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class HindsightMemory:
    def __init__(
        self,
        cfg: Settings = default_settings,
        *,
        event_log: EventLog,
        fallback: LocalMemoryStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.events = event_log
        self.fallback = fallback
        headers = {"Content-Type": "application/json", "User-Agent": "field-service-copilot/1.0"}
        if cfg.hindsight_api_key:
            headers["Authorization"] = f"Bearer {cfg.hindsight_api_key}"
        self._http = httpx.AsyncClient(base_url=cfg.hindsight_base_url, headers=headers, transport=transport, timeout=30.0)
        self._reflect_cache: dict[tuple[str, str, str], tuple[float, ReflectOutcome]] = {}
        self._background: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.cfg.hindsight_enabled

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self._http.aclose()

    # ----------------------------------------------------------------- transport
    async def _request(self, method: str, path: str, body: dict[str, Any] | None, budget_s: float) -> Any:
        """One HTTP call under a hard wall-clock budget. Raises TimeoutError or HindsightError."""

        async def call() -> Any:
            try:
                resp = await self._http.request(method, path, json=body, timeout=budget_s)
            except httpx.TimeoutException as exc:
                raise TimeoutError(str(exc) or "timed out") from exc
            except httpx.HTTPError as exc:
                raise HindsightError(f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code >= 400:
                retry_after = resp.headers.get("retry-after")
                try:
                    retry_after_s = float(retry_after) if retry_after else None
                except ValueError:
                    retry_after_s = None
                raise HindsightError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code, retry_after_s)
            return resp.json() if resp.content else {}

        return await asyncio.wait_for(call(), timeout=budget_s)

    # --------------------------------------------------------------------- recall
    async def recall_context(
        self,
        bank_id: str,
        query: str,
        context_tags: list[str] | None = None,
        *,
        run_id: str | None = None,
        types: list[str] | None = None,
        budget: str = "mid",
        max_tokens: int = 2048,
        tags_match: str = "any",
        limit: int = 12,
        timeout_s: float | None = None,
        label: str = "",
    ) -> RecallOutcome:
        """Retrieve past failure causes, unit repair history and peer technician findings."""
        body: dict[str, Any] = {
            "query": query,
            "types": types or ["world", "experience", "observation"],
            "budget": budget,
            "max_tokens": max_tokens,
            "query_timestamp": now_iso(),
        }
        if context_tags:
            body["tags"] = context_tags
            body["tags_match"] = tags_match
        budget_s = timeout_s or self.cfg.hindsight_timeout_s
        path = f"{API_PREFIX}/{bank_id}/memories/recall"
        start = time.perf_counter()

        if not self.enabled:
            return self._recall_fallback(bank_id, body, run_id, start, "skipped", "HINDSIGHT_API_KEY not configured", limit, label)
        try:
            raw = await self._request("POST", path, body, budget_s)
        except TimeoutError:
            return self._recall_fallback(bank_id, body, run_id, start, "timeout", f"recall exceeded {budget_s:.1f}s budget", limit, label)
        except HindsightError as exc:
            return self._recall_fallback(bank_id, body, run_id, start, "error", str(exc), limit, label)

        hits = [MemoryHit.from_hindsight(r) for r in (raw.get("results") or [])][:limit]
        for hit in hits:
            hit.via = label
        latency = _elapsed_ms(start)
        event = self.events.add(
            MemoryEvent(
                op="recall", bank_id=bank_id, status="ok", source="hindsight", latency_ms=latency,
                request=body, response=raw, run_id=run_id,
                summary=f"{label + ': ' if label else ''}{len(hits)} memories in {latency} ms",
            )
        )
        return RecallOutcome("hindsight", "ok", hits, latency, event.id)

    def _recall_fallback(
        self, bank_id: str, body: dict[str, Any], run_id: str | None, start: float, status: str, reason: str, limit: int, label: str
    ) -> RecallOutcome:
        hits = self.fallback.search(body["query"], body.get("tags"), limit=limit)
        for hit in hits:
            hit.via = label
        latency = _elapsed_ms(start)
        event = self.events.add(
            MemoryEvent(
                op="recall", bank_id=bank_id, status="fallback", source="local_fallback", latency_ms=latency,
                request=body, response={"results": [h.to_dict() for h in hits]}, error=f"{status}: {reason}", run_id=run_id,
                summary=f"{label + ': ' if label else ''}{len(hits)} memories from local fallback ({status})",
            )
        )
        log.info("recall fell back to local store (%s): %s", status, reason)
        return RecallOutcome("local_fallback", status, hits, latency, event.id, error=reason)

    async def fix_outcome_hits(
        self, bank_id: str, model_key: str, error_code: str, *, query_hint: str = "", run_id: str | None = None
    ) -> tuple[list[MemoryHit], RecallOutcome, str]:
        """Outcome-bearing memories for one model+error, used for hold-rate statistics.

        Returns (hits, recall, stats_source). Statistics need the structured outcome metadata we
        retain with every record; if Hindsight's facts come back without it, or recall fell back,
        the write-through ledger (which mirrors every retained record) supplies the numbers."""
        outcome = await self.recall_context(
            bank_id,
            f"repair outcomes for {error_code}: which fixes held and which did not {query_hint}".strip(),
            [model_tag(model_key), error_tag(error_code)],
            run_id=run_id,
            types=["world", "experience"],
            tags_match="all_strict",
            max_tokens=4096,
            limit=60,
            label="verify_fix_outcome",
        )
        if outcome.source == "hindsight" and any(h.action_taken and h.outcome_held is not None for h in outcome.hits):
            return outcome.hits, outcome, "hindsight"
        return self.fallback.outcome_hits(model_key, error_code), outcome, "local_ledger"

    # --------------------------------------------------------------------- retain
    async def retain_interaction(
        self,
        bank_id: str,
        technician_id: str,
        unit_id: str,
        error_code: str,
        action_taken: str,
        outcome_held: bool | None,
        *,
        record: RepairRecord,
        run_id: str | None = None,
    ) -> RetainOutcome:
        """Retain one structured interaction. `record` carries the full context; the
        positional arguments are asserted against it so call sites stay honest."""
        if (record.technician_id, record.unit_id, record.error_code, record.action_taken, record.outcome_held) != (
            technician_id, unit_id, error_code, action_taken, outcome_held,
        ):
            raise ValueError("retain_interaction arguments do not match the record")
        self.invalidate_reflection(bank_id, record.model_key, record.error_code)
        return await self.retain_records(bank_id, [record], run_id=run_id)

    async def retain_records(
        self,
        bank_id: str,
        records: list[RepairRecord],
        *,
        run_id: str | None = None,
        journal: bool = True,
        timeout_s: float | None = None,
        retry_in_background: bool = True,
        retries: int = 0,
        async_: bool = True,
    ) -> RetainOutcome:
        """Retain a batch. Journals locally first so the fallback store always has it.

        `async_=True` (default) lets Hindsight extract facts in the background and return an
        operation id immediately, which is what keeps interactive retains inside the time budget."""
        if journal:
            for record in records:
                self.fallback.add(record)
        body = {"items": [r.to_retain_item() for r in records], "async": async_}
        doc_ids = [r.document_id for r in records]
        path = f"{API_PREFIX}/{bank_id}/memories"
        budget_s = timeout_s or self.cfg.hindsight_timeout_s
        start = time.perf_counter()

        if not self.enabled:
            event = self.events.add(
                MemoryEvent(
                    op="retain", bank_id=bank_id, status="skipped", source="local_fallback", latency_ms=0,
                    request=body, error="HINDSIGHT_API_KEY not configured; stored in local journal only", run_id=run_id,
                    summary=f"{len(records)} record(s) journaled locally",
                )
            )
            return RetainOutcome("skipped", "local_fallback", event.id, len(records), document_ids=doc_ids,
                                 error="Hindsight not configured")

        attempt = 0
        while True:
            try:
                raw = await self._request("POST", path, body, budget_s)
                break
            except (TimeoutError, HindsightError) as exc:
                status = getattr(exc, "status", None)
                retryable = isinstance(exc, TimeoutError) or status in (429, 500, 502, 503, 504)
                if retryable and attempt < retries:
                    wait = getattr(exc, "retry_after", None) or 1.5 * (2 ** attempt)
                    await asyncio.sleep(random.uniform(0.5, 1.0) * wait)
                    attempt += 1
                    continue
                reason = f"retain exceeded {budget_s:.1f}s budget" if isinstance(exc, TimeoutError) else str(exc)
                event = self.events.add(
                    MemoryEvent(
                        op="retain", bank_id=bank_id, status="deferred", source="local_fallback",
                        latency_ms=_elapsed_ms(start), request=body, error=reason, run_id=run_id,
                        summary=f"{len(records)} record(s) journaled; Hindsight retain deferred",
                    )
                )
                if retry_in_background and retryable:
                    self._spawn(self._retry_retain(event.id, path, body))
                return RetainOutcome("deferred", "local_fallback", event.id, len(records), error=reason, document_ids=doc_ids)

        op_id = raw.get("operation_id") or next(iter(raw.get("operation_ids") or []), None)
        event = self.events.add(
            MemoryEvent(
                op="retain", bank_id=bank_id, status="queued" if raw.get("async") else "ok", source="hindsight",
                latency_ms=_elapsed_ms(start), request=body, response=raw, run_id=run_id,
                summary=f"{len(records)} record(s) accepted by Hindsight" + (f" (op {op_id})" if op_id else ""),
            )
        )
        return RetainOutcome(event.status, "hindsight", event.id, len(records), operation_id=op_id, document_ids=doc_ids)

    async def _retry_retain(self, event_id: int, path: str, body: dict[str, Any]) -> None:
        for attempt in range(3):
            await asyncio.sleep(2 * (2 ** attempt) * random.uniform(0.7, 1.3))
            try:
                raw = await self._request("POST", path, body, 20.0)
            except (TimeoutError, HindsightError) as exc:
                self.events.update(event_id, error=f"background retry {attempt + 1}/3 failed: {exc}")
                continue
            self.events.update(event_id, status="queued", source="hindsight", response=raw, error=None,
                               summary=f"{len(body['items'])} record(s) accepted by Hindsight after background retry")
            return

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -------------------------------------------------------------------- reflect
    def invalidate_reflection(self, bank_id: str, model_key: str, error_code: str) -> None:
        self._reflect_cache.pop((bank_id, model_key, error_code), None)

    async def reflect_patterns(
        self,
        bank_id: str,
        unit_model: str,
        error_code: str,
        *,
        model_name: str,
        error_title: str,
        run_id: str | None = None,
        use_cache: bool = True,
    ) -> ReflectOutcome:
        """Ask Hindsight to synthesize diagnostic guidance when manual steps keep failing."""
        key = (bank_id, unit_model, error_code)
        cached = self._reflect_cache.get(key)
        if use_cache and cached and cached[0] > time.time():
            result = ReflectOutcome(**{**cached[1].__dict__, "cached": True})
            result.event_id = self.events.add(
                MemoryEvent(op="reflect", bank_id=bank_id, status="ok", source=result.source, latency_ms=0,
                            request={"cache_key": list(key)}, response={"text": result.text}, run_id=run_id,
                            summary=f"reflection for {error_code} served from cache")
            ).id
            return result

        body: dict[str, Any] = {
            "query": (
                f"What have technicians learned about {model_name} error {error_code} ({error_title})? "
                "Which OEM manual procedures failed, which field fixes held and how often, who discovered them and when, "
                "and do site conditions change the root cause? Give concrete guidance for the next technician."
            ),
            "budget": "mid",
            "max_tokens": 1024,
            "tags": [model_tag(unit_model), error_tag(error_code)],
            "tags_match": "any",
            "include": {"facts": {}},
        }
        path = f"{API_PREFIX}/{bank_id}/reflect"
        budget_s = self.cfg.hindsight_reflect_timeout_s
        start = time.perf_counter()

        if not self.enabled:
            return self._reflect_fallback(bank_id, body, run_id, start, unit_model, error_code, model_name, error_title,
                                          "skipped", "HINDSIGHT_API_KEY not configured")
        try:
            raw = await self._request("POST", path, body, budget_s)
        except TimeoutError:
            return self._reflect_fallback(bank_id, body, run_id, start, unit_model, error_code, model_name, error_title,
                                          "timeout", f"reflect exceeded {budget_s:.0f}s budget")
        except HindsightError as exc:
            return self._reflect_fallback(bank_id, body, run_id, start, unit_model, error_code, model_name, error_title,
                                          "error", str(exc))

        based_on = [
            {"id": m.get("id"), "text": m.get("text"), "type": m.get("type"), "occurred_start": m.get("occurred_start")}
            for m in ((raw.get("based_on") or {}).get("memories") or [])
        ]
        latency = _elapsed_ms(start)
        event = self.events.add(
            MemoryEvent(op="reflect", bank_id=bank_id, status="ok", source="hindsight", latency_ms=latency,
                        request=body, response=raw, run_id=run_id,
                        summary=f"synthesized {error_code} guidance from {len(based_on)} memories")
        )
        result = ReflectOutcome("hindsight", "ok", raw.get("text", ""), based_on, latency, event.id)
        self._reflect_cache[key] = (time.time() + REFLECT_CACHE_TTL_S, result)
        return result

    def _reflect_fallback(
        self, bank_id: str, body: dict[str, Any], run_id: str | None, start: float,
        model_key: str, error_code: str, model_name: str, error_title: str, status: str, reason: str,
    ) -> ReflectOutcome:
        hits = self.fallback.outcome_hits(model_key, error_code)
        text = render_local_synthesis(summarize_outcomes(hits), model_name, error_code, error_title)
        based_on = [{"id": h.id, "text": h.text[:280], "type": h.type, "occurred_start": h.occurred_at} for h in hits[:12]]
        latency = _elapsed_ms(start)
        event = self.events.add(
            MemoryEvent(op="reflect", bank_id=bank_id, status="fallback", source="local_fallback", latency_ms=latency,
                        request=body, response={"text": text}, error=f"{status}: {reason}", run_id=run_id,
                        summary=f"local synthesis for {error_code} from {len(hits)} records ({status})")
        )
        return ReflectOutcome("local_fallback", status, text, based_on, latency, event.id, error=reason)

    # ---------------------------------------------------------------- bank admin
    async def ensure_bank(self, schema: BankSchema, *, timeout_s: float = 15.0) -> dict[str, Any]:
        start = time.perf_counter()
        body = schema.profile_payload()
        try:
            raw = await self._request("PUT", f"{API_PREFIX}/{schema.bank_id}", body, timeout_s)
        except (TimeoutError, HindsightError) as exc:
            self.events.add(MemoryEvent(op="bank", bank_id=schema.bank_id, status="error", source="hindsight",
                                        latency_ms=_elapsed_ms(start), request=body, error=str(exc)))
            raise
        self.events.add(MemoryEvent(op="bank", bank_id=schema.bank_id, status="ok", source="hindsight",
                                    latency_ms=_elapsed_ms(start), request=body, response=raw,
                                    summary="bank profile created/updated"))
        return raw

    async def health(self, bank_id: str) -> dict[str, Any]:
        if not self.enabled:
            return {"reachable": False, "reason": "HINDSIGHT_API_KEY not configured"}
        start = time.perf_counter()
        try:
            stats = await self._request("GET", f"{API_PREFIX}/{bank_id}/stats", None, 4.0)
        except (TimeoutError, HindsightError) as exc:
            return {"reachable": False, "reason": str(exc)[:200], "latency_ms": _elapsed_ms(start)}
        return {"reachable": True, "latency_ms": _elapsed_ms(start), "stats": stats}


__all__ = [
    "HindsightMemory",
    "HindsightError",
    "RecallOutcome",
    "RetainOutcome",
    "ReflectOutcome",
    "context_tags",
]
