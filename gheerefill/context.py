"""Context-window protection and token economy by rule-based reduction (no model summarisation).

The full transcript is kept on disk; only the request view is reduced. Every request resends the
view, so a task's input tokens grow with the square of its length unless old material leaves the
view. Two mechanisms, both deterministic by position and advanced in steps (so the provider's
cached prefix changes only once per step, not on every request):

- an observation window: only the newest tool outputs are shown verbatim; older ones become a
  one-line pointer to their verbatim archive (read_output retrieves them), and large arguments of
  old tool calls (write_file contents, long edits) shrink to their size, keys kept;
- old reasoning of thinking models is cut to a stub.

Under real pressure (the window's limit is near) reductions go further: they are sticky, and the
system prompt, the issue and the newest messages are never reduced.
"""

from __future__ import annotations

import json
from typing import Any

from gheerefill.models.base import Usage

CHARS_PER_TOKEN = 3.2  # conservative for code-heavy text
ARG_KEEP_CHARS = 300  # string arguments longer than this are elided from old tool calls


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "") + len(m.get("reasoning") or "") + 16
        for c in m.get("tool_calls") or []:
            chars += len(c.get("arguments") or "") + len(c.get("name") or "") + 16
    return int(chars / CHARS_PER_TOKEN)


def compact_arguments(raw: str) -> str:
    """The same JSON object with long string values replaced by their size (ids and paths stay)."""
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        obj = None
    if not isinstance(obj, dict):
        return json.dumps({"_context": "earlier tool arguments elided"})
    return json.dumps({k: (f"[{len(v)} chars elided]" if isinstance(v, str) and len(v) > ARG_KEEP_CHARS else v)
                       for k, v in obj.items()})


def _step_cut(count: int, keep: int, step: int) -> int:
    """How many of `count` items (oldest first) fall outside a window of `keep` newest, advanced in
    steps of `step` (0 when the window is off)."""
    if keep <= 0:
        return 0
    return max(0, count - keep) // step * step


class ContextManager:
    # Thinking models return long reasoning every turn and thinking-mode APIs take it back on tool-call
    # turns; beyond the newest turns it is cut to a stub, with the cut-off advancing in steps.
    REASONING_KEEP_TURNS = 2
    REASONING_STEP = 4
    REASONING_STUB_CHARS = 300

    def __init__(self, context_window: int, max_output_tokens: int, reduce_at: float, keep_recent: int,
                 fixed_overhead_tokens: int = 0, observation_window: int = 0, window_step: int = 4):
        # the output reservation never takes more than half the window (thinking models ask for a lot)
        self.limit = context_window - min(max_output_tokens, context_window // 2) - fixed_overhead_tokens
        self.reduce_at = reduce_at
        self.keep_recent = keep_recent
        self.observation_window = observation_window
        self.window_step = max(1, window_step)
        self.elided: set[int] = set()
        self.truncated_text: set[int] = set()
        self.elided_tool_args: set[int] = set()
        self.ratio = 1.0  # provider-observed tokens / our estimate
        self.reductions = 0
        self.pressure = 0  # raised after a context-overflow error
        self.last_estimate = 0
        self.peak_estimate = 0
        self.windowed = 0  # tool outputs currently outside the observation window
        self.trimmed: dict[int, int] = {}  # newest outputs cut to fit the window: index -> chars kept

    def observe(self, view: list[dict[str, Any]], usage: Usage) -> None:
        if usage.known and usage.prompt_tokens > 0:
            est = estimate_tokens(view)
            if est > 0:
                self.ratio = max(0.5, min(3.0, usage.prompt_tokens / est))

    def estimate(self, messages: list[dict[str, Any]]) -> int:
        return int(estimate_tokens(messages) * self.ratio)

    def stats(self) -> dict[str, int | float]:
        """Context facts for the result record and token-efficiency evaluation."""
        return {
            "limit_tokens": self.limit,
            "last_estimate_tokens": self.last_estimate,
            "peak_estimate_tokens": self.peak_estimate,
            "reductions": self.reductions,
            "elided_tool_outputs": len(self.elided),
            "outside_observation_window": self.windowed,
            "truncated_assistant_messages": len(self.truncated_text),
            "elided_tool_arguments": len(self.elided_tool_args),
            "calibration_ratio": round(self.ratio, 3),
            "overflow_pressure": self.pressure,
        }

    def _render(self, transcript: list[dict[str, Any]], protected: int = 2) -> list[dict[str, Any]]:
        assistants = [i for i, m in enumerate(transcript) if m.get("role") == "assistant"]
        old_reasoning = set(assistants[:_step_cut(len(assistants), self.REASONING_KEEP_TURNS, self.REASONING_STEP)])
        tools = [i for i, m in enumerate(transcript) if m.get("role") == "tool" and i >= protected]
        cut = _step_cut(len(tools), self.observation_window, self.window_step)
        stale_before = tools[cut] if cut < len(tools) else len(transcript)
        stale_tools = set(tools[:cut])
        self.windowed = sum(1 for i in stale_tools if len(transcript[i].get("content") or "") > 300)
        out = []
        for i, m in enumerate(transcript):
            r = m.get("reasoning")
            if r and i in old_reasoning and len(r) > self.REASONING_STUB_CHARS:
                m = {**m, "reasoning": r[:self.REASONING_STUB_CHARS] + " [... earlier reasoning truncated ...]"}
            if i in self.elided or (i in stale_tools and len(m.get("content") or "") > 300):
                ref = m.get("output_id")
                src = m.get("source_output")
                hint = (f' read_output(id="{ref}") retrieves it.' if ref else
                        f' It was a view of output {src}.' if src else "")
                out.append({**m, "content": f"[earlier tool output elided ({len(m.get('content') or '')} chars).{hint}]"})
            elif i in self.trimmed and len(m.get("content") or "") > self.trimmed[i]:
                text, keep = m.get("content") or "", self.trimmed[i]
                ref = f' read_output(id="{m["output_id"]}") retrieves them' if m.get("output_id") else ""
                out.append({**m, "content": text[:keep * 2 // 3] + f"\n[... {len(text) - keep} chars cut to fit the "
                            f"context window;{ref} ...]\n" + text[-(keep // 3):]})
            elif i in self.truncated_text:
                text = m.get("content") or ""
                out.append({**m, "content": text[:600] + ("\n[... earlier reasoning truncated ...]" if len(text) > 600 else "")})
            else:
                out.append(m)
            calls = out[-1].get("tool_calls")
            if calls and (i in self.elided_tool_args or (i < stale_before and m.get("role") == "assistant")):
                # Old tool-call arguments: the model has the result; replaying a whole file it wrote buys
                # nothing. Keys (and ids) stay, so the exchange remains valid for every API.
                compact = [{**c, "arguments": compact_arguments(c.get("arguments"))}
                           if len(c.get("arguments") or "") > ARG_KEEP_CHARS else c for c in calls]
                if compact != calls:
                    out[-1] = {**out[-1], "tool_calls": compact}
        return out

    def force_reduce(self, rejected_tokens: int | None = None) -> None:
        """The provider rejected a request as too long: lower the effective window to below the
        rejected size (the profile's context_window may be wrong) and raise reduction pressure."""
        self.pressure += 1
        if rejected_tokens and rejected_tokens > 0:
            self.limit = min(self.limit, int(rejected_tokens * 0.9))

    def _done(self, view: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.last_estimate = self.estimate(view)
        self.peak_estimate = max(self.peak_estimate, self.last_estimate)
        return view

    def prepare(self, transcript: list[dict[str, Any]], protected: int = 2) -> list[dict[str, Any]]:
        """Return the request view. `protected` leading messages (system + issue) are never reduced."""
        threshold = self.limit * max(0.2, self.reduce_at - 0.15 * self.pressure)
        view = self._render(transcript, protected)
        estimate = self.estimate(view)
        self.peak_estimate = max(self.peak_estimate, estimate)  # the size before any pressure reduction
        if estimate <= threshold:
            return self._done(view)
        self.reductions += 1
        target = threshold * 0.6
        n = len(transcript)
        recent_start = max(protected, n - self.keep_recent)
        # pass 1: elide old tool outputs (oldest first)
        for i in range(protected, recent_start):
            if transcript[i]["role"] == "tool" and i not in self.elided and len(transcript[i].get("content") or "") > 300:
                self.elided.add(i)
                view = self._render(transcript, protected)
                if self.estimate(view) <= target:
                    return self._done(view)
        # pass 2: truncate old assistant text and compact old tool-call arguments
        for i in range(protected, recent_start):
            m = transcript[i]
            if m["role"] == "assistant":
                if len(m.get("content") or "") > 600:
                    self.truncated_text.add(i)
                if any(len(c.get("arguments") or "") > ARG_KEEP_CHARS for c in m.get("tool_calls") or []):
                    self.elided_tool_args.add(i)
        view = self._render(transcript, protected)
        if self.estimate(view) <= threshold:
            return self._done(view)
        # pass 3: shrink the recent window too; the last two messages (the newest call and its result)
        # stay intact whatever the pressure
        for i in range(recent_start, max(recent_start, n - 2)):
            m = transcript[i]
            if m["role"] == "tool" and len(m.get("content") or "") > 300:
                self.elided.add(i)
            elif m["role"] == "assistant":
                if len(m.get("content") or "") > 600:
                    self.truncated_text.add(i)
                if any(len(c.get("arguments") or "") > ARG_KEEP_CHARS for c in m.get("tool_calls") or []):
                    self.elided_tool_args.add(i)
        view = self._render(transcript, protected)
        # pass 4: the request must fit the window itself. Compact the newest call's arguments, then cut
        # the newest outputs down to what fits (head and tail kept) instead of dropping them.
        if self.estimate(view) > self.limit:
            for i in range(max(protected, n - 2), n):
                if transcript[i]["role"] == "assistant":
                    self.elided_tool_args.add(i)
            view = self._render(transcript, protected)
            excess = self.estimate(view) - int(self.limit * 0.9)
            for i in range(n - 1, max(protected, n - 2) - 1, -1):
                content = transcript[i].get("content") or ""
                if excess <= 0 or transcript[i]["role"] != "tool" or len(content) < 600:
                    continue
                keep = max(400, len(content) - int(excess * CHARS_PER_TOKEN / max(self.ratio, 0.5)) - 200)
                self.trimmed[i] = keep
                excess -= int((len(content) - keep) / CHARS_PER_TOKEN * self.ratio)
            view = self._render(transcript, protected)
        return self._done(view)
