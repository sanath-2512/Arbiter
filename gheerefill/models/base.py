"""Provider-neutral model types, failure classification and deadline-aware retry.

Internal transcript messages are plain JSON-serialisable dicts:

    {"role": "system"|"user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": str}],
     "provider_raw": <provider-specific content needed for faithful replay, optional>}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

from __future__ import annotations

import email.utils
import json
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


class ErrorClass(str, Enum):
    AUTH = "authentication"
    QUOTA = "quota_exhausted"
    RATE_LIMIT = "rate_limited"
    SERVER = "server_error"
    UNSUPPORTED = "unsupported_parameter"
    MALFORMED_REQUEST = "malformed_request"
    CONTEXT_OVERFLOW = "context_overflow"
    TIMEOUT = "timeout"
    NETWORK = "network_error"
    BAD_RESPONSE = "bad_response"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    CONTENT_FILTER = "content_filter"  # provider moderation rejected the input (DashScope data_inspection_failed)


TRANSIENT = {ErrorClass.RATE_LIMIT, ErrorClass.SERVER, ErrorClass.TIMEOUT, ErrorClass.NETWORK, ErrorClass.BAD_RESPONSE}
# transient classes that are retried for a time window, not only a fixed number of times: the provider
# answered, so it is reachable but busy (a wrong or unreachable endpoint still fails after max_attempts)
WINDOWED = {ErrorClass.RATE_LIMIT, ErrorClass.SERVER}


class ModelError(Exception):
    def __init__(
        self,
        cls: ErrorClass,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        usage_uncertain: bool = False,
        body: str = "",
    ):
        super().__init__(f"{cls.value}: {message}")
        self.cls = cls
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s
        self.usage_uncertain = usage_uncertain
        self.body = body[:2000]

    @property
    def transient(self) -> bool:
        return self.cls in TRANSIENT


@dataclass
class Usage:
    """Normalised usage. `input_tokens` EXCLUDES cache reads/writes (no double counting);
    `reasoning_tokens` is informational (already included in output_tokens where the
    provider says so). `raw` keeps the provider fields verbatim."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    known: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "known": self.known,
            "raw": self.raw,
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str
    parse_error: str | None = None


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: Usage
    provider_raw: Any = None
    latency_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    # Chain of thought returned separately (DeepSeek/Qwen `reasoning_content`, or inline <think>).
    # Kept in the transcript because thinking-mode APIs require it back on tool-call turns.
    reasoning: str = ""

    def to_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": self.text,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.raw_arguments} for c in self.tool_calls],
        }
        if self.reasoning:
            msg["reasoning"] = self.reasoning
        if self.provider_raw is not None:
            msg["provider_raw"] = self.provider_raw
        return msg


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


class ModelClient(Protocol):
    provider: str
    model_name: str

    def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, timeout_s: float,
                 total_s: float | None = None) -> ModelTurn:
        """Single attempt. Raises ModelError."""
        ...


# ---------------------------------------------------------------------------
# HTTP error classification (pure; unit tested)

_CONTEXT_PATTERNS = re.compile(
    r"context[_ ]length|maximum context|context window|prompt is too long|too many tokens|"
    r"input is too long|reduce the length|max_tokens.*exceed|exceeds? the (model'?s? )?(maximum|limit)|"
    r"input tokens exceed|token limit|range of input length|input length should be",
    re.I,
)
_UNSUPPORTED_PATTERNS = re.compile(
    r"unsupported|not supported|unrecognized|unknown (parameter|field|argument)|extra (inputs|fields) (are )?not permitted|"
    r"does not support|unexpected keyword|invalid_request_error.*(param|field)|is not allowed",
    re.I,
)
_QUOTA_PATTERNS = re.compile(r"insufficient_quota|quota|billing|credit|payment required|spend limit|"
                             r"insufficient (account )?balance|arrearage|overdue", re.I)
_CONTENT_FILTER = re.compile(r"data_?inspection_?failed|inappropriate content|content exists risk|content_filter|"
                             r"content management policy|sensitive content|safety (system|check) (rejected|blocked)", re.I)
_OUTPUT_PARAM = re.compile(r"max[_ ]?(completion_|output_|new_)?tokens|output tokens|completion tokens", re.I)
_OUTPUT_LIMITS = (
    re.compile(r"at most (\d{3,7})", re.I),                                     # OpenAI
    re.compile(r"> ?(\d{3,7}),? which is the maximum", re.I),                    # Anthropic
    re.compile(r"range of max_tokens is \[\s*\d+\s*,\s*(\d{3,7})\s*\]", re.I),  # DeepSeek
    re.compile(r"less than or equal to `?(\d{3,7})", re.I),                       # Groq
    re.compile(r"maximum (?:value|allowed|number|output)[^0-9]{0,60}?(\d{3,7})", re.I),
    re.compile(r"(?:<=|≤) ?`?(\d{3,7})", re.I),
)


def output_token_limit(message: str, current: int) -> int | None:
    """If a provider rejected the requested output-token budget and names its limit, that limit.
    (A model whose cap is below the profile's max_output_tokens would otherwise fail every request.)"""
    if not _OUTPUT_PARAM.search(message):
        return None
    for pat in _OUTPUT_LIMITS:
        for m in pat.finditer(message):
            n = int(m.group(1))
            if 256 <= n < current:
                return n
    return None


def parse_retry_after(headers: dict[str, str], now: float | None = None) -> float | None:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if "retry-after-ms" in h:
        try:
            return max(0.0, float(h["retry-after-ms"]) / 1000.0)
        except ValueError:
            pass
    if "retry-after" in h:
        v = h["retry-after"].strip()
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(v)
                return max(0.0, dt.timestamp() - (now if now is not None else time.time()))
            except (TypeError, ValueError):
                return None
    return None


def classify_http_error(status: int, body: str, headers: dict[str, str] | None = None) -> ModelError:
    headers = headers or {}
    retry_after = parse_retry_after(headers)
    msg = body
    try:
        parsed = json.loads(body)
        err = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
        if isinstance(err, dict):
            msg = " ".join(str(err.get(k, "")) for k in ("type", "code", "message") if err.get(k)) or body
        elif isinstance(err, str):
            msg = err
    except (json.JSONDecodeError, AttributeError):
        pass
    msg = (msg or f"HTTP {status}").strip()[:1000]
    if status == 403 and _CONTENT_FILTER.search(msg):
        return ModelError(ErrorClass.CONTENT_FILTER, msg, status=status, body=body)
    if status in (401, 403):
        return ModelError(ErrorClass.AUTH, msg, status=status, body=body)
    if status == 402 or (status == 429 and _QUOTA_PATTERNS.search(msg)):
        return ModelError(ErrorClass.QUOTA, msg, status=status, body=body)
    if status == 429:
        return ModelError(ErrorClass.RATE_LIMIT, msg, status=status, retry_after_s=retry_after, body=body)
    if status in (400, 403, 422, 451) and _CONTENT_FILTER.search(msg):
        return ModelError(ErrorClass.CONTENT_FILTER, msg, status=status, body=body)
    if status == 413 or (status in (400, 422) and _CONTEXT_PATTERNS.search(msg)):
        return ModelError(ErrorClass.CONTEXT_OVERFLOW, msg, status=status, body=body)
    if status in (400, 404, 422) and _UNSUPPORTED_PATTERNS.search(msg):
        return ModelError(ErrorClass.UNSUPPORTED, msg, status=status, body=body)
    if status == 408:
        return ModelError(ErrorClass.TIMEOUT, msg, status=status, retry_after_s=retry_after, usage_uncertain=True, body=body)
    if status >= 500 or status == 409:
        # 5xx (incl. Anthropic 529 overloaded) may or may not have been billed.
        return ModelError(
            ErrorClass.SERVER, msg, status=status, retry_after_s=retry_after, usage_uncertain=True, body=body
        )
    return ModelError(ErrorClass.MALFORMED_REQUEST, msg, status=status, body=body)


# ---------------------------------------------------------------------------
# Retry


@dataclass
class AttemptRecord:
    attempt: int
    started_at: float
    latency_s: float
    outcome: str  # "ok" or ErrorClass value
    status: int | None
    usage: dict[str, Any] | None
    usage_uncertain: bool
    message: str = ""
    slept_s: float = 0.0


TOTAL_FACTOR = 4  # longest single response = 4 x request_timeout_s (20 min at the default 300 s)


def call_with_retry(
    client: ModelClient,
    messages: list[dict[str, Any]],
    tools: list[ToolSpec],
    *,
    max_attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    time_left: Callable[[], float],
    request_timeout_s: float,
    on_attempt: Callable[[AttemptRecord], None],
    rng: random.Random,
    sleep: Callable[[float], None] = time.sleep,
    min_call_s: float = 5.0,
    transient_window_s: float = 0.0,
) -> ModelTurn:
    """Bounded, deadline-aware retries for transient failures only.

    `time_left()` returns seconds available for model work (deadline minus the
    finalisation reserve). Permanent failures raise immediately. A provider retry hint is
    honoured when it fits inside the remaining time; otherwise the call gives up with
    ErrorClass.DEADLINE rather than sleeping past the deadline.

    Rate limits and server errors (overloaded, 5xx) are retried beyond `max_attempts` while
    less than `transient_window_s` has passed since the first attempt: a provider rate-limiting a
    shared key for a few minutes should not end a task that has most of its budget left.
    """
    first = time.monotonic()
    waited = 0.0  # time slept between attempts (counts even when `sleep` is simulated)
    attempt = 0
    while True:
        attempt += 1
        remaining = time_left()
        if remaining < min_call_s:
            raise ModelError(ErrorClass.DEADLINE, f"insufficient time for a model call ({remaining:.1f}s left)")
        # request_timeout_s bounds each wait for data; a thinking model's whole answer may take longer
        # (it streams, or keeps the connection alive), up to TOTAL_FACTOR x that, within the deadline
        timeout = min(request_timeout_s, remaining)
        total = min(remaining, max(timeout, TOTAL_FACTOR * request_timeout_s))
        t0 = time.monotonic()
        started = time.time()
        try:
            turn = client.complete(messages, tools, timeout_s=timeout, total_s=total)
        except ModelError as e:
            latency = time.monotonic() - t0
            on_attempt(
                AttemptRecord(attempt, started, latency, e.cls.value, e.status, None, e.usage_uncertain, e.message[:300])
            )
            if not e.transient or (attempt >= max_attempts and not (
                    e.cls in WINDOWED and max(time.monotonic() - first, waited) < transient_window_s)):
                raise
            delay = min(max_delay_s, base_delay_s * (2 ** min(attempt - 1, 16)))
            delay = delay * (0.5 + rng.random() * 0.5)
            if e.retry_after_s is not None:
                delay = max(delay, e.retry_after_s)
            if delay > time_left() - min_call_s:
                raise ModelError(
                    ErrorClass.DEADLINE,
                    f"retry after {delay:.1f}s would exceed the deadline; last error: {e}",
                ) from e
            sleep(delay)
            waited += delay
            continue
        except Exception as e:  # a client bug or an unexpected library error: the request may have been billed
            if not getattr(e, "recorded_by_caller", False):
                on_attempt(AttemptRecord(attempt, started, time.monotonic() - t0, "client_exception", None, None, True,
                                         f"{type(e).__name__}: {e}"[:300]))
            raise
        latency = time.monotonic() - t0
        turn.latency_s = latency
        on_attempt(AttemptRecord(attempt, started, latency, "ok", 200, turn.usage.to_dict(), False))
        return turn
