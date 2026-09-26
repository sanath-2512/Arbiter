"""Context-window protection by rule-based reduction (no model summarisation).

The full transcript is kept on disk; only the request view is reduced. Reductions are
sticky (an elided message stays elided) so the request prefix stays stable for provider
prompt caching. The system prompt, the issue and recent messages are never elided.
Elided tool outputs keep a pointer to their verbatim archive for exact retrieval.
"""

from __future__ import annotations

from typing import Any

from gheerefill.models.base import Usage

CHARS_PER_TOKEN = 3.2  # conservative for code-heavy text


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "") + len(m.get("reasoning") or "") + 16
        for c in m.get("tool_calls") or []:
            chars += len(c.get("arguments") or "") + len(c.get("name") or "") + 16
    return int(chars / CHARS_PER_TOKEN)


class ContextManager:
    # Thinking models return long reasoning every turn and thinking-mode APIs take it back on tool-call
    # turns; beyond the newest turns it is cut to a stub. The cut-off advances in steps of
    # REASONING_STEP turns: a sliding cut-off would change one message per request and void the
    # provider's prompt cache for every turn after it; stepped, the cached prefix changes once per step.
    REASONING_KEEP_TURNS = 4
    REASONING_STEP = 8
    REASONING_STUB_CHARS = 400

    def __init__(self, context_window: int, max_output_tokens: int, reduce_at: float, keep_recent: int,
                 fixed_overhead_tokens: int = 0):
        # the output reservation never takes more than half the window (thinking models ask for a lot)
        self.limit = context_window - min(max_output_tokens, context_window // 2) - fixed_overhead_tokens
        self.reduce_at = reduce_at
        self.keep_recent = keep_recent
        self.elided: set[int] = set()
        self.truncated_text: set[int] = set()
        self.ratio = 1.0  # provider-observed tokens / our estimate
        self.reductions = 0
        self.pressure = 0  # raised after a context-overflow error

    def observe(self, view: list[dict[str, Any]], usage: Usage) -> None:
        if usage.known and usage.prompt_tokens > 0:
            est = estimate_tokens(view)
            if est > 0:
                self.ratio = max(0.5, min(3.0, usage.prompt_tokens / est))

    def estimate(self, messages: list[dict[str, Any]]) -> int:
        return int(estimate_tokens(messages) * self.ratio)

    def _render(self, transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        assistants = [i for i, m in enumerate(transcript) if m.get("role") == "assistant"]
        cut = max(0, len(assistants) - self.REASONING_KEEP_TURNS) // self.REASONING_STEP * self.REASONING_STEP
        old_reasoning = set(assistants[:cut])
        for i, m in enumerate(transcript):
            r = m.get("reasoning")
            if r and i in old_reasoning and len(r) > self.REASONING_STUB_CHARS:
                m = {**m, "reasoning": r[:self.REASONING_STUB_CHARS] + " [... earlier reasoning truncated ...]"}
            if i in self.elided:
                ref = m.get("output_id")
                src = m.get("source_output")
                hint = (f' Retrieve verbatim with read_output(id="{ref}").' if ref else
                        f' It was a view of output {src}.' if src else "")
                out.append({**m, "content": f"[earlier tool output elided to save context ({len(m.get('content') or '')} chars).{hint}]"})
            elif i in self.truncated_text:
                text = m.get("content") or ""
                out.append({**m, "content": text[:600] + ("\n[... earlier reasoning truncated ...]" if len(text) > 600 else "")})
            else:
                out.append(m)
        return out

    def force_reduce(self, rejected_tokens: int | None = None) -> None:
        """The provider rejected a request as too long: lower the effective window to below the
        rejected size (the profile's context_window may be wrong) and raise reduction pressure."""
        self.pressure += 1
        if rejected_tokens and rejected_tokens > 0:
            self.limit = min(self.limit, int(rejected_tokens * 0.9))

    def prepare(self, transcript: list[dict[str, Any]], protected: int = 2) -> list[dict[str, Any]]:
        """Return the request view. `protected` leading messages (system + issue) are never reduced."""
        threshold = self.limit * max(0.2, self.reduce_at - 0.15 * self.pressure)
        view = self._render(transcript)
        if self.estimate(view) <= threshold:
            return view
        self.reductions += 1
        target = threshold * 0.6
        n = len(transcript)
        recent_start = max(protected, n - self.keep_recent)
        # pass 1: elide old tool outputs (oldest first)
        for i in range(protected, recent_start):
            if transcript[i]["role"] == "tool" and i not in self.elided and len(transcript[i].get("content") or "") > 300:
                self.elided.add(i)
                view = self._render(transcript)
                if self.estimate(view) <= target:
                    return view
        # pass 2: truncate old assistant reasoning text
        for i in range(protected, recent_start):
            if transcript[i]["role"] == "assistant" and len(transcript[i].get("content") or "") > 600:
                self.truncated_text.add(i)
        view = self._render(transcript)
        if self.estimate(view) <= threshold:
            return view
        # pass 3: shrink the recent window too (keep the last 2 messages intact)
        for i in range(recent_start, max(recent_start, n - 2)):
            if transcript[i]["role"] == "tool" and len(transcript[i].get("content") or "") > 300:
                self.elided.add(i)
        return self._render(transcript)
