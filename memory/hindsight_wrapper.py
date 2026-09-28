"""Hindsight integration layer: retain, recall and reflect with graceful degradation.

Talks to the Hindsight REST API directly (the same endpoints `hindsight-client`
wraps) so every call gets a hard wall-clock budget and the exact request/response
can be shown in the Memory Inspector.

Latency and degradation design (measured against Hindsight Cloud: recall
~0.7-2.2 s, reflect ~15 s):

* recall  - answered within HINDSIGHT_TIMEOUT_S (1.5 s) or from the local fallback
            store. A timed-out call is *shielded*, not cancelled: it finishes in the
            background and warms a short-lived cache, and the UI can prefetch the same
            recall while the technician is still typing.
* retain  - journaled locally first (the outbox), then delivered by a background task under
            its own budget (HINDSIGHT_RETAIN_TIMEOUT_S per attempt, retried with backoff). A
            retain happens after the answer is shown, so it never holds up a technician and
            is never cut off by the 1.5 s recall budget. Anything Hindsight has not
            acknowledged is re-sent by the outbox sync loop.
* reflect - never blocks a diagnosis: served from cache, prewarmed at startup and
            refreshed when new outcomes arrive, with a deterministic local synthesis
            in the meantime.
The agent pipeline never raises because memory is slow or unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from config import Settings, settings as default_settings
from memory.bank_schemas import BankSchema, RepairRecord, error_tag, model_tag, now_iso, resolve_bank_id
from memory.event_log import EventLog, MemoryEvent
from memory.fallback_store import LocalMemoryStore
from memory.insights import MemoryHit, render_local_synthesis, summarize_outcomes
from memory.outbox import AckStore

log = logging.getLogger("copilot.memory")

API_PREFIX = "/v1/default/banks"
RECALL_CACHE_TTL_S = 180
REFLECT_CACHE_TTL_S = 1800
LATE_RESULT_GRACE_S = 15.0  # how long a timed-out recall may keep running to warm the cache
KEEP_WARM_INTERVAL_S = 45
OUTBOX_SYNC_INTERVAL_S = 60
RETAIN_RETRIES = 2  # three attempts in total, each under HINDSIGHT_RETAIN_TIMEOUT_S
RETAIN_BACKOFF_S = 1.5
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


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
    label: str = ""
    cache_age_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "event_id": self.event_id,
            "error": self.error,
            "label": self.label,
            "cache_age_s": self.cache_age_s,
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
    directives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _recall_key(bank_id: str, body: dict[str, Any]) -> str:
    """Cache identity for a recall: everything that changes the result, not the timestamp."""
    return json.dumps(
        {
            "bank": bank_id,
            "query": body["query"],
            "types": sorted(body.get("types") or []),
            "tags": sorted(body.get("tags") or []),
            "tags_match": body.get("tags_match"),
            "budget": body.get("budget"),
            "max_tokens": body.get("max_tokens"),
        },
        sort_keys=True,
    )


class HindsightMemory:
    def __init__(
        self,
        cfg: Settings = default_settings,
        *,
        event_log: EventLog,
        fallback: LocalMemoryStore,
        transport: httpx.AsyncBaseTransport | None = None,
        acks_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.events = event_log
        self.fallback = fallback
        headers = {"Content-Type": "application/json", "User-Agent": "field-service-copilot/1.1"}
        if cfg.hindsight_api_key:
            headers["Authorization"] = f"Bearer {cfg.hindsight_api_key}"
        self._http = httpx.AsyncClient(
            base_url=cfg.hindsight_base_url,
            headers=headers,
            transport=transport,
            timeout=30.0,
            # Keep TLS connections alive between technician queries; the default 5 s expiry
            # would put a fresh handshake on the critical path of almost every recall.
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120),
        )
        self._acks = AckStore(acks_path) if acks_path else None
        self._recall_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._recall_inflight: dict[str, asyncio.Task] = {}
        self._recall_late: dict[str, str] = {}  # in-flight recalls whose caller already fell back
        self._reflect_cache: dict[tuple[str, str, str], tuple[float, ReflectOutcome]] = {}
        self._reflect_inflight: dict[tuple[str, str, str], asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        self._sync_lock = asyncio.Lock()
        self._inflight_docs: set[str] = set()  # retains being delivered right now (not outbox work)

    @property
    def enabled(self) -> bool:
        return self.cfg.hindsight_enabled

    # ------------------------------------------------------------- lifecycle
    def start_background(self, *, bank_ids: list[str], prewarm: list[dict[str, str]] | None = None) -> None:
        """Keep the connection warm, sync the outbox, and prewarm reflections."""
        if not self.enabled:
            return
        self._spawn(self._keep_warm_loop())
        self._spawn(self._outbox_loop(bank_ids))
        if prewarm and self.cfg.hindsight_prewarm:
            self._spawn(self.prewarm_reflections(prewarm))

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self._http.aclose()

    def _spawn(self, coro: Any) -> asyncio.Task | None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return None
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _keep_warm_loop(self) -> None:
        while True:
            try:
                await self._send("GET", "/health", None, 5.0)
            except (TimeoutError, HindsightError):
                pass
            await asyncio.sleep(KEEP_WARM_INTERVAL_S)

    # ------------------------------------------------------------- transport
    async def _send(self, method: str, path: str, body: dict[str, Any] | None, timeout_s: float) -> Any:
        """One HTTP call. Raises TimeoutError or HindsightError; never leaks the auth header."""
        try:
            resp = await self._http.request(method, path, json=body, timeout=timeout_s)
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
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:  # a proxy error page must degrade like any other failure
            raise HindsightError(f"invalid JSON from Hindsight: {exc}", resp.status_code) from exc

    async def _request(self, method: str, path: str, body: dict[str, Any] | None, budget_s: float) -> Any:
        """_send under a hard wall-clock budget (connect + TLS + server time)."""
        return await asyncio.wait_for(self._send(method, path, body, budget_s), timeout=budget_s)

    # ----------------------------------------------------------------- recall
    def recall_body(
        self,
        query: str,
        tags: list[str] | None,
        *,
        types: list[str] | None = None,
        budget: str = "mid",
        max_tokens: int = 2048,
        tags_match: str = "any",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "types": types or ["world", "experience", "observation"],
            "budget": budget,
            "max_tokens": max_tokens,
            "query_timestamp": now_iso(),
        }
        if tags:
            body["tags"] = tags
            body["tags_match"] = tags_match
        return body

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
        limit: int | None = 12,
        timeout_s: float | None = None,
        label: str = "",
        use_cache: bool = True,
    ) -> RecallOutcome:
        """Retrieve past failure causes, unit repair history and peer technician findings."""
        body = self.recall_body(query, context_tags, types=types, budget=budget, max_tokens=max_tokens, tags_match=tags_match)
        budget_s = timeout_s or self.cfg.hindsight_timeout_s
        start = time.perf_counter()
        if not self.enabled:
            return self._recall_fallback(bank_id, body, run_id, start, "skipped", "HINDSIGHT_API_KEY not configured", limit, label)

        key = _recall_key(bank_id, body)
        cached = self._recall_cache.get(key) if use_cache else None
        if cached and time.time() - cached[0] < RECALL_CACHE_TTL_S:
            age = time.time() - cached[0]
            return self._recall_result(bank_id, body, cached[1], run_id, start, "cached", label, limit, cache_age_s=age)

        task = self._recall_inflight.get(key) or self._start_recall(key, bank_id, body, label)
        try:
            raw = await asyncio.wait_for(asyncio.shield(task), timeout=budget_s)
        except TimeoutError:
            self._recall_late[key] = label
            return self._recall_fallback(bank_id, body, run_id, start, "timeout",
                                         f"recall exceeded {budget_s:.1f}s budget; Hindsight result will warm the cache",
                                         limit, label)
        except HindsightError as exc:
            return self._recall_fallback(bank_id, body, run_id, start, "error", str(exc), limit, label)
        return self._recall_result(bank_id, body, raw, run_id, start, "ok", label, limit)

    def _start_recall(self, key: str, bank_id: str, body: dict[str, Any], label: str) -> asyncio.Task:
        started = time.perf_counter()

        async def fetch() -> dict[str, Any]:
            raw = await self._send("POST", f"{API_PREFIX}/{bank_id}/memories/recall", body, LATE_RESULT_GRACE_S)
            self._recall_cache[key] = (time.time(), raw)
            return raw

        task = asyncio.get_running_loop().create_task(fetch())
        self._recall_inflight[key] = task

        def done(t: asyncio.Task) -> None:
            self._recall_inflight.pop(key, None)
            if t.cancelled():
                return
            exc = t.exception()  # always retrieve, so orphaned failures are not logged as unhandled
            late = self._recall_late.pop(key, None)
            if late is not None and exc is None:
                self.events.add(MemoryEvent(
                    op="recall", bank_id=bank_id, status="ok", source="hindsight", latency_ms=_elapsed_ms(started),
                    request=body, response={"results": len((t.result() or {}).get("results") or [])},
                    summary=f"{late}: late Hindsight result cached for the next request",
                ))

        task.add_done_callback(done)
        return task

    def prefetch(self, bank_id: str, bodies: list[dict[str, Any]]) -> int:
        """Start recalls without waiting so a diagnosis moments later is served from cache."""
        if not self.enabled:
            return 0
        started = 0
        for body in bodies:
            key = _recall_key(bank_id, body)
            cached = self._recall_cache.get(key)
            if key in self._recall_inflight or (cached and time.time() - cached[0] < RECALL_CACHE_TTL_S / 2):
                continue
            self._start_recall(key, bank_id, body, "prefetch")
            started += 1
        return started

    def _recall_result(
        self, bank_id: str, body: dict[str, Any], raw: dict[str, Any], run_id: str | None, start: float,
        status: str, label: str, limit: int | None, cache_age_s: float | None = None,
    ) -> RecallOutcome:
        hits = [MemoryHit.from_hindsight(r) for r in (raw.get("results") or [])]
        hits = hits[:limit] if limit else hits
        for hit in hits:
            hit.via = label
        latency = _elapsed_ms(start)
        note = f"served from cache ({cache_age_s:.0f}s old)" if cache_age_s is not None else f"in {latency} ms"
        event = self.events.add(
            MemoryEvent(
                op="recall", bank_id=bank_id, status="ok" if status == "ok" else "cached", source="hindsight",
                latency_ms=latency, request=body, response=raw, run_id=run_id,
                summary=f"{label + ': ' if label else ''}{len(hits)} memories {note}",
            )
        )
        return RecallOutcome("hindsight", status, hits, latency, event.id, label=label, cache_age_s=cache_age_s)

    def _recall_fallback(
        self, bank_id: str, body: dict[str, Any], run_id: str | None, start: float, status: str, reason: str,
        limit: int | None, label: str,
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
        return RecallOutcome("local_fallback", status, hits, latency, event.id, error=reason, label=label)

    def outcome_recall_body(self, model_key: str, error_code: str, error_title: str, model_name: str) -> dict[str, Any]:
        """The one recall that feeds both the prompt and the hold-rate statistics for a fault."""
        return self.recall_body(
            f"{model_name} {error_code} {error_title}: field root causes, OEM procedures that failed, fixes that held",
            [model_tag(model_key), error_tag(error_code)],
            types=["world", "experience", "observation"],
            tags_match="all_strict",
            max_tokens=4096,
        )

    async def fix_outcome_hits(
        self, bank_id: str, model_key: str, error_code: str, *, error_title: str = "", model_name: str = "",
        run_id: str | None = None,
    ) -> tuple[list[MemoryHit], RecallOutcome, str]:
        """Outcome-bearing memories for one model+error, used for hold-rate statistics.

        Returns (hits, recall, stats_source). Statistics need the structured outcome metadata we
        retain with every record; if Hindsight's facts come back without it, or recall fell back,
        the write-through ledger (which mirrors every retained record) supplies the numbers."""
        body = self.outcome_recall_body(model_key, error_code, error_title, model_name)
        outcome = await self.recall_context(
            bank_id, body["query"], body["tags"], run_id=run_id, types=body["types"], tags_match="all_strict",
            max_tokens=body["max_tokens"], limit=None, label="fleet history",
        )
        if outcome.source == "hindsight" and any(h.action_taken and h.outcome_held is not None for h in outcome.hits):
            return outcome.hits, outcome, "hindsight"
        return self.fallback.outcome_hits(model_key, error_code), outcome, "local_ledger"

    # ----------------------------------------------------------------- retain
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
        wait_s: float | None = None,
    ) -> RetainOutcome:
        """Retain one structured interaction. `record` carries the full context; the
        positional arguments are asserted against it so call sites stay honest.
        `wait_s` bounds how long to wait for Hindsight's acknowledgement (see retain_records)."""
        if (record.technician_id, record.unit_id, record.error_code, record.action_taken, record.outcome_held) != (
            technician_id, unit_id, error_code, action_taken, outcome_held,
        ):
            raise ValueError("retain_interaction arguments do not match the record")
        result = await self.retain_records(bank_id, [record], run_id=run_id, wait_s=wait_s)
        if outcome_held is not None:
            # A confirmed outcome changes the pattern: refresh the reflection once Hindsight has it.
            self.invalidate_reflection(bank_id, record.model_key, record.error_code)
            self._spawn(self._refresh_reflection_later(bank_id, record.model_key, record.error_code,
                                                       record.model_name, record.error_title))
        return result

    async def retain_records(
        self,
        bank_id: str,
        records: list[RepairRecord],
        *,
        run_id: str | None = None,
        journal: bool = True,
        timeout_s: float | None = None,
        retries: int | None = None,
        async_: bool = True,
        wait_s: float | None = None,
    ) -> RetainOutcome:
        """Retain a batch: journal locally first (the outbox), then deliver to Hindsight.

        Delivery runs as its own task under the retain budget (HINDSIGHT_RETAIN_TIMEOUT_S per
        attempt, retried with backoff), never the 1.5 s interactive recall budget: a retain happens
        after the answer is on screen, so a slow write must neither block nor "fail" a diagnosis.

        `wait_s` bounds how long the caller waits for Hindsight's acknowledgement (None: until
        delivery finishes). If it is not back in time the outcome is 'sending' and delivery completes
        in the background; the outbox guarantees the record lands even across restarts.
        `async_=True` lets Hindsight extract facts after acknowledging, which keeps the write fast."""
        if journal:
            for record in records:
                self.fallback.add(record)
        self._recall_cache.clear()  # new knowledge: cached recalls may be stale
        body = {"items": [r.to_retain_item() for r in records], "async": async_}
        doc_ids = [r.document_id for r in records]

        if not self.enabled:
            event = self.events.add(
                MemoryEvent(
                    op="retain", bank_id=bank_id, status="skipped", source="local_fallback", latency_ms=0,
                    request=body, error="HINDSIGHT_API_KEY not configured; kept in the local outbox", run_id=run_id,
                    summary=f"{len(records)} record(s) journaled locally",
                )
            )
            return RetainOutcome("skipped", "local_fallback", event.id, len(records), document_ids=doc_ids,
                                 error="Hindsight not configured")

        event = self.events.add(
            MemoryEvent(
                op="retain", bank_id=bank_id, status="sending", source="hindsight", latency_ms=0,
                request=body, run_id=run_id, summary=f"{len(records)} record(s) sending to Hindsight",
            )
        )
        self._inflight_docs.update(doc_ids)
        task = self._spawn(self._deliver(
            event.id, bank_id, body, doc_ids, journal=journal,
            timeout_s=timeout_s or self.cfg.hindsight_retain_timeout_s,
            retries=RETAIN_RETRIES if retries is None else retries,
        ))
        if task is None:  # no running event loop: leave it to the outbox
            self._inflight_docs.difference_update(doc_ids)
            return RetainOutcome("deferred", "local_fallback", event.id, len(records), document_ids=doc_ids,
                                 error="no event loop for delivery")
        try:
            if wait_s is None:
                return await asyncio.shield(task)
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
        except TimeoutError:
            # Still in flight: the answer is not held up; delivery finishes in the background.
            return RetainOutcome("sending", "hindsight", event.id, len(records), document_ids=doc_ids)

    async def _deliver(
        self, event_id: int, bank_id: str, body: dict[str, Any], doc_ids: list[str], *,
        journal: bool, timeout_s: float, retries: int,
    ) -> RetainOutcome:
        """POST a retain with retries and jittered backoff. Never raises; updates its event in place."""
        path = f"{API_PREFIX}/{bank_id}/memories"
        n = len(body["items"])
        start = time.perf_counter()
        attempt = 0
        try:
            while True:
                try:
                    raw = await self._request("POST", path, body, timeout_s)
                    break
                except (TimeoutError, HindsightError) as exc:
                    status = getattr(exc, "status", None)
                    retryable = isinstance(exc, TimeoutError) or status in RETRYABLE_STATUS
                    reason = f"retain attempt exceeded {timeout_s:.0f}s" if isinstance(exc, TimeoutError) else str(exc)[:300]
                    if retryable and attempt < retries:
                        wait = getattr(exc, "retry_after", None) or RETAIN_BACKOFF_S * (2 ** attempt)
                        self.events.update(event_id, error=f"attempt {attempt + 1} failed ({reason}); retrying")
                        await asyncio.sleep(random.uniform(0.5, 1.0) * wait)
                        attempt += 1
                        continue
                    self.events.update(
                        event_id, status="deferred", source="local_fallback", latency_ms=_elapsed_ms(start),
                        error=reason, summary=f"{n} record(s) kept in the outbox; sync will retry",
                    )
                    if journal and retryable:
                        self._spawn(self._sync_soon([bank_id], delay_s=30.0))
                    return RetainOutcome("deferred", "local_fallback", event_id, n, error=reason, document_ids=doc_ids)
        finally:
            self._inflight_docs.difference_update(doc_ids)

        op_id = raw.get("operation_id") or next(iter(raw.get("operation_ids") or []), None)
        if journal and self._acks is not None:
            self._acks.add(doc_ids, bank_id=bank_id, operation_id=op_id)
        status = "queued" if raw.get("async") else "ok"
        latency = _elapsed_ms(start)
        retried = f" after {attempt} retr{'y' if attempt == 1 else 'ies'}" if attempt else ""
        self.events.update(
            event_id, status=status, source="hindsight", latency_ms=latency, response=raw, error=None,
            summary=f"{n} record(s) accepted by Hindsight in {latency} ms{retried}" + (f" (op {op_id})" if op_id else ""),
        )
        return RetainOutcome(status, "hindsight", event_id, n, operation_id=op_id, document_ids=doc_ids)

    # ----------------------------------------------------------------- outbox
    def pending_records(self) -> list[RepairRecord]:
        """Journaled records Hindsight has not acknowledged, excluding ones being delivered right now."""
        if self._acks is None:
            return []
        return [
            r for r in self.fallback.runtime_records()
            if r.document_id not in self._acks and r.document_id not in self._inflight_docs
        ]

    def outbox_status(self) -> dict[str, Any]:
        pending = self.pending_records()
        return {
            "pending": len(pending),
            "enabled": self.enabled,
            "oldest": min((r.occurred_at for r in pending), default=None),
        }

    async def sync_outbox(self, bank_ids: list[str], *, batch_size: int = 10) -> dict[str, Any]:
        """Push every journaled record Hindsight has not acknowledged yet."""
        if not self.enabled or self._acks is None:
            return {"pushed": 0, "failed": 0, **self.outbox_status()}
        pushed = failed = 0
        async with self._sync_lock:
            pending = self.pending_records()
            by_bank: dict[str, list[RepairRecord]] = {}
            for record in pending:
                bank = resolve_bank_id(record.fleet) if len(bank_ids) > 1 else bank_ids[0]
                by_bank.setdefault(bank, []).append(record)
            for bank, records in by_bank.items():
                for i in range(0, len(records), batch_size):
                    batch = records[i : i + batch_size]
                    result = await self.retain_records(bank, batch, journal=False, timeout_s=20.0, retries=1)
                    if result.source == "hindsight":
                        self._acks.add([r.document_id for r in batch], bank_id=bank, operation_id=result.operation_id)
                        pushed += len(batch)
                    else:
                        failed += len(batch)
        return {"pushed": pushed, "failed": failed, **self.outbox_status()}

    async def _sync_soon(self, bank_ids: list[str], delay_s: float = 10.0) -> None:
        await asyncio.sleep(delay_s)
        await self.sync_outbox(bank_ids)

    async def _outbox_loop(self, bank_ids: list[str]) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                if self.pending_records():
                    result = await self.sync_outbox(bank_ids)
                    if result["pushed"]:
                        log.info("outbox: synced %d record(s) to Hindsight", result["pushed"])
            except Exception:  # the loop must survive anything
                log.exception("outbox sync failed")
            await asyncio.sleep(OUTBOX_SYNC_INTERVAL_S)

    # ---------------------------------------------------------------- reflect
    def invalidate_reflection(self, bank_id: str, model_key: str, error_code: str) -> None:
        self._reflect_cache.pop((bank_id, model_key, error_code), None)

    def cached_reflection(self, bank_id: str, model_key: str, error_code: str) -> ReflectOutcome | None:
        cached = self._reflect_cache.get((bank_id, model_key, error_code))
        return cached[1] if cached and cached[0] > time.time() else None

    def _reflect_body(self, model_key: str, error_code: str, model_name: str, error_title: str) -> dict[str, Any]:
        return {
            "query": (
                f"What have technicians learned about {model_name} error {error_code} ({error_title})? "
                "Which OEM manual procedures failed, which field fixes held and how often, who discovered them and when, "
                "and do site conditions change the root cause? Give concrete guidance for the next technician."
            ),
            "budget": "low",
            "max_tokens": 900,
            "tags": [model_tag(model_key), error_tag(error_code)],
            "tags_match": "any",
            "include": {"facts": {}},
        }

    def _start_reflect(self, bank_id: str, model_key: str, error_code: str, model_name: str, error_title: str,
                       run_id: str | None) -> asyncio.Task:
        key = (bank_id, model_key, error_code)
        existing = self._reflect_inflight.get(key)
        if existing:
            return existing
        body = self._reflect_body(model_key, error_code, model_name, error_title)

        async def fetch() -> ReflectOutcome:
            start = time.perf_counter()
            raw = await self._send("POST", f"{API_PREFIX}/{bank_id}/reflect", body, self.cfg.hindsight_reflect_timeout_s)
            based = raw.get("based_on") or {}
            based_on = [
                {"id": m.get("id"), "text": m.get("text"), "type": m.get("type"), "occurred_start": m.get("occurred_start")}
                for m in based.get("memories") or []
            ]
            directives = [d.get("name") for d in based.get("directives") or [] if d.get("name")]
            latency = _elapsed_ms(start)
            event = self.events.add(
                MemoryEvent(op="reflect", bank_id=bank_id, status="ok", source="hindsight", latency_ms=latency,
                            request=body, response=raw, run_id=run_id,
                            summary=f"synthesized {error_code} guidance from {len(based_on)} memories"
                                    + (f" under {len(directives)} directives" if directives else ""))
            )
            result = ReflectOutcome("hindsight", "ok", raw.get("text", ""), based_on, latency, event.id, directives=directives)
            self._reflect_cache[key] = (time.time() + REFLECT_CACHE_TTL_S, result)
            return result

        task = asyncio.get_running_loop().create_task(fetch())
        self._reflect_inflight[key] = task

        def done(t: asyncio.Task) -> None:
            self._reflect_inflight.pop(key, None)
            if not t.cancelled() and t.exception() is not None:
                self.events.add(MemoryEvent(op="reflect", bank_id=bank_id, status="error", source="hindsight",
                                            latency_ms=0, request=body, error=str(t.exception())[:300], run_id=run_id,
                                            summary=f"background reflect for {error_code} failed"))

        task.add_done_callback(done)
        return task

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
        wait_s: float | None = None,
    ) -> ReflectOutcome:
        """Synthesize diagnostic guidance when manual steps fail repeatedly.

        Waits at most `wait_s` (default: the full reflect budget). If Hindsight is still
        thinking, a deterministic local synthesis is returned and the Hindsight result lands
        in the cache for the next request."""
        key = (bank_id, unit_model, error_code)
        cached = self.cached_reflection(*key) if use_cache else None
        if cached:
            result = ReflectOutcome(**{**cached.__dict__, "cached": True})
            result.event_id = self.events.add(
                MemoryEvent(op="reflect", bank_id=bank_id, status="cached", source="hindsight", latency_ms=0,
                            request={"cache_key": list(key)}, response={"text": result.text}, run_id=run_id,
                            summary=f"reflection for {error_code} served from cache")
            ).id
            result.latency_ms = 0
            return result

        start = time.perf_counter()
        body = self._reflect_body(unit_model, error_code, model_name, error_title)
        if not self.enabled:
            return self._reflect_fallback(bank_id, body, run_id, start, unit_model, error_code, model_name, error_title,
                                          "skipped", "HINDSIGHT_API_KEY not configured")
        task = self._start_reflect(bank_id, unit_model, error_code, model_name, error_title, run_id)
        wait = self.cfg.hindsight_reflect_timeout_s if wait_s is None else wait_s
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        except TimeoutError:
            return self._reflect_fallback(bank_id, body, run_id, start, unit_model, error_code, model_name, error_title,
                                          "pending", "Hindsight reflect is still running; its result will be cached")
        except (HindsightError, Exception) as exc:  # noqa: BLE001 - any reflect failure degrades to local synthesis
            return self._reflect_fallback(bank_id, body, run_id, start, unit_model, error_code, model_name, error_title,
                                          "error", str(exc)[:300])

    async def prewarm_reflections(self, pairs: list[dict[str, str]]) -> None:
        """Warm the reflect cache one pair at a time (reflect is heavy; don't stampede the API)."""
        for pair in pairs:
            key = (pair["bank_id"], pair["model_key"], pair["error_code"])
            if self.cached_reflection(*key):
                continue
            task = self._start_reflect(pair["bank_id"], pair["model_key"], pair["error_code"],
                                       pair["model_name"], pair["error_title"], run_id="prewarm")
            try:
                await task
            except Exception:  # noqa: BLE001 - logged by the task's done-callback
                pass

    async def _refresh_reflection_later(self, bank_id: str, model_key: str, error_code: str, model_name: str,
                                        error_title: str, delay_s: float = 20.0) -> None:
        await asyncio.sleep(delay_s)  # give Hindsight time to extract the new outcome first
        self.invalidate_reflection(bank_id, model_key, error_code)
        await self.prewarm_reflections([{"bank_id": bank_id, "model_key": model_key, "error_code": error_code,
                                         "model_name": model_name, "error_title": error_title}])

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

    async def reflect_structured(
        self, bank_id: str, query: str, tags: list[str], schema: dict[str, Any], *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Reflect with a JSON schema (Hindsight structured output). Raises on failure."""
        body = {"query": query, "budget": "low", "max_tokens": 1200, "tags": tags, "tags_match": "any",
                "response_schema": schema, "include": {"facts": {}}}
        start = time.perf_counter()
        try:
            raw = await self._request("POST", f"{API_PREFIX}/{bank_id}/reflect", body,
                                      timeout_s or self.cfg.hindsight_reflect_timeout_s)
        except (TimeoutError, HindsightError) as exc:
            self.events.add(MemoryEvent(op="reflect", bank_id=bank_id, status="error", source="hindsight",
                                        latency_ms=_elapsed_ms(start), request=body, error=str(exc)[:300],
                                        summary="structured reflect failed"))
            raise
        latency = _elapsed_ms(start)
        self.events.add(MemoryEvent(op="reflect", bank_id=bank_id, status="ok", source="hindsight", latency_ms=latency,
                                    request=body, response=raw, summary="structured reflect (bulletin draft)"))
        if raw.get("structured_output") is None:
            raise HindsightError(f"no structured output: {raw.get('structured_output_error') or 'unknown error'}")
        based = raw.get("based_on") or {}
        return {
            "structured": raw["structured_output"],
            "text": raw.get("text", ""),
            "memories": len(based.get("memories") or []),
            "directives": [d.get("name") for d in based.get("directives") or [] if d.get("name")],
            "latency_ms": latency,
        }

    # ------------------------------------------------------------- bank admin
    async def _bank_step(self, bank_id: str, summary: str, method: str, path: str, body: dict[str, Any] | None,
                         timeout_s: float) -> Any:
        start = time.perf_counter()
        try:
            raw = await self._request(method, path, body, timeout_s)
        except (TimeoutError, HindsightError) as exc:
            self.events.add(MemoryEvent(op="bank", bank_id=bank_id, status="error", source="hindsight",
                                        latency_ms=_elapsed_ms(start), request=body or {}, error=str(exc),
                                        summary=summary))
            raise
        self.events.add(MemoryEvent(op="bank", bank_id=bank_id, status="ok", source="hindsight",
                                    latency_ms=_elapsed_ms(start), request=body or {}, response=raw, summary=summary))
        return raw

    async def ensure_bank(self, schema: BankSchema, *, timeout_s: float = 20.0) -> dict[str, Any]:
        """Create the bank if needed, apply its config, and sync its directives. Idempotent."""
        bank = f"{API_PREFIX}/{schema.bank_id}"
        await self._bank_step(schema.bank_id, "bank created/updated", "PUT", bank, schema.create_payload(), timeout_s)
        await self._bank_step(schema.bank_id, "bank config patched", "PATCH", f"{bank}/config",
                              {"updates": schema.config_updates()}, timeout_s)

        listing = await self._request("GET", f"{bank}/directives", None, timeout_s)
        existing = {d["name"]: d for d in (listing.get("items") or [])}
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        for directive in schema.directives:
            current = existing.get(directive.name)
            if current is None:
                await self._bank_step(schema.bank_id, f"directive '{directive.name}' created", "POST",
                                      f"{bank}/directives", directive.payload(), timeout_s)
                counts["created"] += 1
            elif (current.get("content"), current.get("priority"), current.get("is_active")) != (
                directive.content, directive.priority, True,
            ):
                await self._bank_step(schema.bank_id, f"directive '{directive.name}' updated", "PATCH",
                                      f"{bank}/directives/{current['id']}", directive.payload(), timeout_s)
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
        return {"bank_id": schema.bank_id, "directives": counts}

    async def operation_status(self, bank_id: str, operation_id: str, *, timeout_s: float = 15.0) -> dict[str, Any]:
        return await self._request("GET", f"{API_PREFIX}/{bank_id}/operations/{operation_id}", None, timeout_s)

    async def bank_stats(self, bank_id: str, *, timeout_s: float = 15.0) -> dict[str, Any]:
        return await self._request("GET", f"{API_PREFIX}/{bank_id}/stats", None, timeout_s)

    async def retain_item(self, bank_id: str, item: dict[str, Any], *, summary: str, timeout_s: float = 20.0) -> dict[str, Any]:
        """Retain a pre-built memory item (e.g. an approved bulletin). Raises on failure."""
        self._recall_cache.clear()
        return await self._bank_step(bank_id, summary, "POST", f"{API_PREFIX}/{bank_id}/memories",
                                     {"items": [item], "async": True}, timeout_s)

    async def upsert_directive(self, bank_id: str, payload: dict[str, Any], *, timeout_s: float = 20.0) -> str:
        """Create a directive, or update the one with the same name. Returns 'created' or 'updated'."""
        existing = {d["name"]: d for d in await self.list_directives(bank_id, timeout_s=timeout_s)}
        current = existing.get(payload["name"])
        if current:
            await self._bank_step(bank_id, f"directive '{payload['name']}' updated", "PATCH",
                                  f"{API_PREFIX}/{bank_id}/directives/{current['id']}", payload, timeout_s)
            return "updated"
        await self._bank_step(bank_id, f"directive '{payload['name']}' created", "POST",
                              f"{API_PREFIX}/{bank_id}/directives", payload, timeout_s)
        return "created"

    async def list_directives(self, bank_id: str, *, timeout_s: float = 10.0) -> list[dict[str, Any]]:
        listing = await self._request("GET", f"{API_PREFIX}/{bank_id}/directives", None, timeout_s)
        return listing.get("items") or []

    async def health(self, bank_id: str) -> dict[str, Any]:
        if not self.enabled:
            return {"reachable": False, "reason": "HINDSIGHT_API_KEY not configured"}
        # Reachability comes from the cheap /health endpoint; bank stats can be slow (they took
        # over 6 s during startup work) and are a nice-to-have, so they never decide "reachable".
        start = time.perf_counter()
        try:
            await self._request("GET", "/health", None, 5.0)
        except (TimeoutError, HindsightError) as exc:
            return {"reachable": False, "reason": str(exc)[:200] or "health check timed out after 5s",
                    "latency_ms": _elapsed_ms(start)}
        latency = _elapsed_ms(start)
        try:
            stats = await self._request("GET", f"{API_PREFIX}/{bank_id}/stats", None, 8.0)
        except (TimeoutError, HindsightError):
            stats = None
        return {
            "reachable": True,
            "latency_ms": latency,
            "stats": {k: stats.get(k) for k in ("total_nodes", "total_documents", "total_observations",
                                                "pending_operations", "last_memory_write_at")} if stats else None,
        }


__all__ = ["HindsightMemory", "HindsightError", "RecallOutcome", "RetainOutcome", "ReflectOutcome"]
