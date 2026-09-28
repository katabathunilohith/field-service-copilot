"""Groq chat-completions client (OpenAI-compatible) hardened for tool calling.

Handles the failure modes that show up in practice with open-weight models:

* 429 / 5xx / connection drops  -> exponential backoff with full jitter, honouring Retry-After
* malformed tool-call arguments  -> `repair_json` (python literals, single quotes, trailing
  commas, code fences, double-encoded strings, unquoted keys, truncated objects)
* Groq `tool_use_failed` 400s    -> recover the call from `failed_generation`, else re-prompt once
* tool calls emitted as text     -> parse <tool_call>…</tool_call>, <function=…>…</function> and bare JSON
* mangled tool names             -> strip `functions.` prefixes / channel tokens, fuzzy-match to known tools
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import json
import logging
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import AsyncOpenAI

from config import Settings, settings as default_settings

log = logging.getLogger("copilot.llm")


class LLMUnavailable(Exception):
    """Raised when no LLM is configured or it keeps failing after retries."""

class ToolArgumentError(ValueError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    repaired: bool = False
    repair_notes: list[str] = field(default_factory=list)

    def to_message(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": json.dumps(self.arguments)}}


@dataclass
class LLMTurn:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: dict[str, int]
    model: str
    latency_ms: int
    attempts: int = 1
    recovered_from: str | None = None
    waited_s: float = 0.0

    def assistant_message(self) -> dict[str, Any]:
        """A clean assistant message for the next request (repaired arguments, no provider extras)."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [tc.to_message() for tc in self.tool_calls]
        return message


# ------------------------------------------------------------------ JSON repair
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_UNQUOTED_KEY = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:')
_PY_LITERALS = ((re.compile(r"\bTrue\b"), "true"), (re.compile(r"\bFalse\b"), "false"), (re.compile(r"\bNone\b"), "null"))


def _balance(text: str) -> tuple[str, bool]:
    """Close an unterminated string and any unclosed braces/brackets (truncated generations)."""
    stack: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    closed = text + ('"' if in_string else "") + "".join(reversed(stack))
    return closed, closed != text


def _first_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]  # truncated: let _balance close it


def repair_json(raw: str | dict | None) -> tuple[dict[str, Any], list[str]]:
    """Parse tool arguments, repairing common LLM mistakes. Returns (arguments, notes)."""
    if isinstance(raw, dict):
        return raw, []
    notes: list[str] = []
    text = (raw or "").strip()
    if not text:
        return {}, ["empty arguments treated as {}"]

    def as_dict(value: Any) -> dict[str, Any] | None:
        if isinstance(value, str):
            try:
                inner = json.loads(value)
            except json.JSONDecodeError:
                return None
            notes.append("decoded double-encoded JSON string")
            value = inner
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            notes.append("unwrapped single-element list")
            value = value[0]
        return value if isinstance(value, dict) else None

    try:
        parsed = as_dict(json.loads(text))
        if parsed is not None:
            return parsed, notes
    except json.JSONDecodeError:
        pass

    if _FENCE.search(text):
        text = _FENCE.sub("", text).strip()
        notes.append("stripped code fence")
    obj = _first_object(text)
    if obj is not None and obj != text:
        notes.append("extracted JSON object from surrounding text")
        text = obj

    # Python-dict syntax (single quotes, True/None, trailing commas) is common enough to try directly.
    try:
        parsed = as_dict(ast.literal_eval(text))
        if parsed is not None:
            notes.append("parsed python-literal syntax")
            return parsed, notes
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass

    candidate = text
    if '\\"' in candidate and candidate.count('"') == candidate.count('\\"'):
        candidate = candidate.replace('\\"', '"')
        notes.append("unescaped over-escaped quotes")
    if "'" in candidate and '"' not in candidate:
        candidate = candidate.replace("'", '"')
        notes.append("converted single quotes")
    for pattern, replacement in _PY_LITERALS:
        if pattern.search(candidate):
            candidate = pattern.sub(replacement, candidate)
            notes.append("converted python literals")
    if _TRAILING_COMMA.search(candidate):
        candidate = _TRAILING_COMMA.sub(r"\1", candidate)
        notes.append("removed trailing commas")
    if _UNQUOTED_KEY.search(candidate):
        candidate = _UNQUOTED_KEY.sub(r'\1"\2":', candidate)
        notes.append("quoted bare keys")
    candidate, balanced = _balance(candidate)
    if balanced:
        notes.append("closed truncated JSON")
        candidate = _TRAILING_COMMA.sub(r"\1", candidate)

    try:
        parsed = as_dict(json.loads(candidate))
    except json.JSONDecodeError as exc:
        raise ToolArgumentError(f"unrepairable tool arguments: {exc.msg} at char {exc.pos}") from exc
    if parsed is None:
        raise ToolArgumentError("tool arguments are not a JSON object")
    return parsed, notes


# ------------------------------------------------------- text-embedded tool calls
_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
_FUNCTION_TAG = re.compile(r"<function=([\w.\-]+)>\s*(.*?)\s*(?:</function>|$)", re.DOTALL)


def normalize_tool_name(name: str, known: list[str]) -> tuple[str, list[str]]:
    notes = []
    clean = name.strip()
    if "<|" in clean:
        clean = clean.split("<|", 1)[0]
        notes.append("stripped channel token from tool name")
    for prefix in ("functions.", "function.", "tools.", "tool."):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            notes.append(f"stripped '{prefix}' prefix")
    clean = clean.strip(" .\"'`")
    if clean not in known and known:
        match = difflib.get_close_matches(clean, known, n=1, cutoff=0.75)
        if match:
            notes.append(f"fuzzy-matched tool name '{clean}' -> '{match[0]}'")
            clean = match[0]
    return clean, notes


def extract_text_tool_calls(content: str, known: list[str]) -> list[tuple[str, str]]:
    """Recover tool calls that a model wrote into `content` instead of `tool_calls`."""
    found: list[tuple[str, str]] = []
    for name, args in _FUNCTION_TAG.findall(content or ""):
        found.append((name, args))
    blobs = _TAGGED.findall(content or "")
    if not found and not blobs:
        stripped = _FENCE.sub("", (content or "").strip())
        if stripped.startswith("{") or stripped.startswith("["):
            blobs = [stripped]
    for blob in blobs:
        try:
            parsed = json.loads(blob)
            items = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            try:
                items = [repair_json(blob)[0]]
            except ToolArgumentError:
                continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or (item.get("function") or {}).get("name")
            args = item.get("arguments", item.get("parameters", (item.get("function") or {}).get("arguments", {})))
            if name and (not known or normalize_tool_name(name, known)[0] in known):
                found.append((name, args if isinstance(args, str) else json.dumps(args)))
    return found


# ------------------------------------------------------------------ rate limit
WaitCallback = Callable[[float, str], Awaitable[None]]


class TokenBucket:
    """Client-side view of Groq's per-model tokens-per-minute budget.

    Groq reports the budget on every response (x-ratelimit-limit-tokens /
    x-ratelimit-remaining-tokens) and refills it continuously. Tracking it lets the
    client wait the few seconds a request needs instead of burning a 429 round trip,
    and lets the UI tell the technician why it is waiting."""

    def __init__(self) -> None:
        self.limit: float | None = None
        self.remaining: float | None = None
        self.updated = time.monotonic()

    def observe(self, headers: Mapping[str, str] | None) -> None:
        if not headers:
            return
        try:
            limit = float(headers.get("x-ratelimit-limit-tokens") or 0) or None
            remaining = headers.get("x-ratelimit-remaining-tokens")
        except (TypeError, ValueError):
            return
        if limit and remaining is not None:
            try:
                self.limit, self.remaining, self.updated = limit, float(remaining), time.monotonic()
            except ValueError:
                return

    def available(self) -> float | None:
        if self.limit is None or self.remaining is None:
            return None
        refill = (time.monotonic() - self.updated) * self.limit / 60.0
        return min(self.limit, self.remaining + refill)

    def reserve(self, tokens: float) -> float:
        """Seconds to wait before `tokens` fit, and book them so concurrent calls see less."""
        available = self.available()
        if available is None or self.limit is None:
            return 0.0
        needed = min(tokens, self.limit * 0.95)
        wait = max(0.0, (needed - available) / (self.limit / 60.0))
        self.remaining, self.updated = available - needed, time.monotonic()
        return wait


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_tokens: int) -> int:
    """Rough prompt+completion estimate (~3.5 chars/token for this English/JSON mix)."""
    chars = len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(tools or []))
    return int(chars / 3.5) + min(max_tokens, 700)


# ----------------------------------------------------------------------- client
class GroqClient:
    RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
    MAX_THROTTLE_WAIT_S = 45.0

    def __init__(self, cfg: Settings = default_settings, *, client: AsyncOpenAI | None = None) -> None:
        self.cfg = cfg
        self.model = cfg.groq_model
        self.baseline_model = cfg.groq_baseline_model or cfg.groq_model
        self._client = client
        if self._client is None and cfg.groq_api_key:
            # SDK retries are disabled so this class owns backoff policy and logging.
            self._client = AsyncOpenAI(api_key=cfg.groq_api_key, base_url=cfg.groq_base_url, max_retries=0, timeout=60.0)
        self._buckets: dict[str, TokenBucket] = {}
        self._bucket_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._client is not None

    def bucket(self, model: str) -> TokenBucket:
        return self._buckets.setdefault(model, TokenBucket())

    def rate_status(self) -> dict[str, Any]:
        return {
            model: {"limit_tpm": b.limit, "available_tokens": round(b.available() or 0)}
            for model, b in self._buckets.items()
            if b.limit
        }

    async def _create(self, **kwargs: Any) -> dict[str, Any] | tuple[dict[str, Any], Mapping[str, str]]:
        """Single raw API call returning (dict, headers). Tests may override it to return just a dict."""
        if self._client is None:
            raise LLMUnavailable("GROQ_API_KEY is not configured")
        raw = await self._client.chat.completions.with_raw_response.create(**kwargs)
        return raw.parse().model_dump(), raw.headers

    async def _throttle(self, model: str, tokens: int, on_wait: WaitCallback | None) -> float:
        async with self._bucket_lock:
            wait = self.bucket(model).reserve(tokens)
        if wait > 0.25:
            wait = min(wait, self.MAX_THROTTLE_WAIT_S)
            log.info("Groq %s token budget: waiting %.1fs for ~%d tokens", model, wait, tokens)
            if on_wait:
                await on_wait(wait, f"Pacing for Groq's tokens-per-minute limit (~{tokens:,} tokens needed)")
            await asyncio.sleep(wait)
            return wait
        return 0.0

    def _backoff(self, attempt: int, exc: Exception | None) -> float:
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            header = response.headers.get("retry-after")
            try:
                retry_after = float(header) if header else None
            except ValueError:
                retry_after = None
        ceiling = retry_after if retry_after is not None else min(20.0, 1.0 * (2 ** attempt))
        return max(0.25, random.uniform(0.5, 1.0) * ceiling)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.2,
        max_tokens: int = 1800,
        model: str | None = None,
        on_wait: WaitCallback | None = None,
    ) -> LLMTurn:
        model = model or self.model
        known = [t["function"]["name"] for t in tools or []]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if model.startswith("openai/gpt-oss") and self.cfg.groq_reasoning_effort:
            kwargs["reasoning_effort"] = self.cfg.groq_reasoning_effort

        start = time.perf_counter()
        nudged = False
        attempt = 0
        waited = 0.0
        while True:
            attempt += 1
            waited += await self._throttle(model, estimate_tokens(kwargs["messages"], tools, max_tokens), on_wait)
            try:
                result = await self._create(**kwargs)
                raw, headers = result if isinstance(result, tuple) else (result, None)
                self.bucket(model).observe(headers)
                turn = self._normalize(raw, known)
                turn.latency_ms = int((time.perf_counter() - start) * 1000)
                turn.attempts = attempt
                turn.waited_s = round(waited, 1)
                return turn
            except openai.BadRequestError as exc:
                error = self._error_body(exc)
                if error.get("code") != "tool_use_failed":
                    raise LLMUnavailable(f"Groq rejected the request: {error.get('message') or exc}") from exc
                recovered = self._recover_failed_generation(error.get("failed_generation") or "", known)
                if recovered:
                    recovered.latency_ms = int((time.perf_counter() - start) * 1000)
                    recovered.attempts = attempt
                    return recovered
                if nudged:
                    raise LLMUnavailable("model repeatedly produced invalid tool calls") from exc
                nudged = True
                log.warning("tool_use_failed; retrying with a corrective instruction")
                kwargs["messages"] = messages + [{
                    "role": "system",
                    "content": "Your previous tool call was malformed. Call tools only with a single valid JSON object as arguments, matching the schema exactly.",
                }]
                kwargs["temperature"] = 0.0
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as exc:
                if attempt > self.cfg.groq_max_retries:
                    raise LLMUnavailable(f"Groq unavailable after {attempt} attempts: {type(exc).__name__}") from exc
                response = getattr(exc, "response", None)
                self.bucket(model).observe(response.headers if response is not None else None)
                wait = self._backoff(attempt - 1, exc)
                log.warning("Groq %s; retrying in %.1fs (attempt %d)", type(exc).__name__, wait, attempt)
                if on_wait and isinstance(exc, openai.RateLimitError):
                    await on_wait(wait, "Groq rate limit reached; retrying")
                waited += wait
                await asyncio.sleep(wait)
            except openai.APIStatusError as exc:
                if exc.status_code in (401, 403):
                    raise LLMUnavailable(f"Groq rejected the API key ({exc.status_code})") from exc
                if exc.status_code in self.RETRYABLE_STATUS and attempt <= self.cfg.groq_max_retries:
                    await asyncio.sleep(self._backoff(attempt - 1, exc))
                    continue
                raise LLMUnavailable(f"Groq error {exc.status_code}") from exc

    @staticmethod
    def _error_body(exc: openai.APIStatusError) -> dict[str, Any]:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            return body.get("error", body) if isinstance(body.get("error", body), dict) else {}
        return {}

    def _recover_failed_generation(self, text: str, known: list[str]) -> LLMTurn | None:
        calls = extract_text_tool_calls(text, known)
        if not calls:
            return None
        tool_calls = self._build_calls([(None, name, args) for name, args in calls], known, source="failed_generation")
        if not tool_calls:
            return None
        return LLMTurn("", tool_calls, "tool_calls", {}, self.model, 0, recovered_from="tool_use_failed")

    def _build_calls(self, raw_calls: list[tuple[str | None, str, Any]], known: list[str], source: str) -> list[ToolCall]:
        calls = []
        for call_id, name, args in raw_calls:
            clean_name, name_notes = normalize_tool_name(name or "", known)
            raw_args = args if isinstance(args, str) else json.dumps(args or {})
            try:
                parsed, notes = repair_json(args)
            except ToolArgumentError as exc:
                # Surface to the orchestrator so the model gets a precise error back.
                parsed, notes = {"__invalid__": str(exc)}, [str(exc)]
            notes = name_notes + notes + ([f"recovered from {source}"] if source != "tool_calls" else [])
            calls.append(ToolCall(call_id or f"call_{secrets.token_hex(6)}", clean_name, parsed, raw_args, bool(notes), notes))
        return calls

    def _normalize(self, raw: dict[str, Any], known: list[str]) -> LLMTurn:
        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        structured = [
            (tc.get("id"), (tc.get("function") or {}).get("name", ""), (tc.get("function") or {}).get("arguments"))
            for tc in message.get("tool_calls") or []
        ]
        tool_calls = self._build_calls(structured, known, "tool_calls")
        recovered_from = None
        if not tool_calls and known and content:
            embedded = extract_text_tool_calls(content, known)
            if embedded:
                tool_calls = self._build_calls([(None, n, a) for n, a in embedded], known, "message content")
                content = ""
                recovered_from = "text_tool_call"
        usage = raw.get("usage") or {}
        return LLMTurn(
            content=content.strip(),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage={k: usage.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            model=raw.get("model") or self.model,
            latency_ms=0,
            recovered_from=recovered_from,
        )

    # ------------------------------------------------------------ speech to text
    async def transcribe(self, audio: bytes, filename: str, content_type: str, *, prompt: str = "") -> dict[str, Any]:
        """Whisper on Groq. `prompt` biases recognition toward fleet vocabulary (unit IDs, codes)."""
        if self._client is None:
            raise LLMUnavailable("GROQ_API_KEY is not configured")
        model = self.cfg.groq_whisper_model
        start = time.perf_counter()
        for attempt in range(1, self.cfg.groq_max_retries + 2):
            try:
                result = await self._client.audio.transcriptions.create(
                    model=model, file=(filename, audio, content_type), prompt=prompt[:800],
                    response_format="verbose_json", temperature=0.0, language="en",
                )
                data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
                return {
                    "text": (data.get("text") or "").strip(),
                    "duration_s": data.get("duration"),
                    "model": model,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                }
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as exc:
                if attempt > self.cfg.groq_max_retries:
                    raise LLMUnavailable(f"Transcription unavailable: {type(exc).__name__}") from exc
                await asyncio.sleep(self._backoff(attempt - 1, exc))
            except openai.APIStatusError as exc:
                raise LLMUnavailable(f"Transcription failed ({exc.status_code}): {self._error_body(exc).get('message', '')}") from exc
        raise LLMUnavailable("Transcription unavailable")
