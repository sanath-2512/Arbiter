"""Deterministic scripted model for tests and replay. NEVER a stand-in for live results:
every result produced with it is labelled `"live": false`.

Script: a JSON list of turns (or {"turns": [...]}). Each turn is one of
  {"text": "...", "tool_calls": [{"name": "bash", "arguments": {...}}],
   "finish_reason": "tool_calls", "usage": {"input_tokens": 10, "output_tokens": 5}}
  {"raw_arguments": "..."}                     # inside a tool call: malformed JSON args
  {"error": "rate_limited", "message": "...", "retry_after_s": 0.0, "usage_uncertain": false}
  {"sleep_s": 1.5, ...}                        # delay before answering (timeouts/cancel tests)
A turn may also be a Python callable (in-process tests) receiving the rendered messages.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Union

from gheerefill.models.base import ErrorClass, ModelError, ModelTurn, ToolCall, ToolSpec, Usage

Turn = Union[Dict[str, Any], Callable[[List[Dict[str, Any]]], Dict[str, Any]]]  # typing forms: runtime alias on 3.9


def load_script(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    turns = data["turns"] if isinstance(data, dict) else data
    if not isinstance(turns, list):
        raise ValueError("fake script must be a list of turns")
    return turns


class FakeClient:
    provider = "fake"

    def __init__(self, turns: list[Turn], model_name: str = "fake-scripted"):
        self.turns = list(turns)
        self.model_name = model_name
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, timeout_s: float) -> ModelTurn:
        self.calls.append(messages)
        if not self.turns:
            raise ModelError(ErrorClass.MALFORMED_REQUEST, "fake script exhausted")
        spec = self.turns.pop(0)
        if callable(spec):
            spec = spec(messages)
        if spec.get("sleep_s"):
            if spec["sleep_s"] > timeout_s:
                time.sleep(timeout_s)
                raise ModelError(ErrorClass.TIMEOUT, "fake timeout", usage_uncertain=True)
            time.sleep(spec["sleep_s"])
        if "error" in spec:
            raise ModelError(
                ErrorClass(spec["error"]),
                spec.get("message", "scripted failure"),
                status=spec.get("status"),
                retry_after_s=spec.get("retry_after_s"),
                usage_uncertain=bool(spec.get("usage_uncertain", False)),
            )
        calls = []
        for c in spec.get("tool_calls", []):
            raw = c["raw_arguments"] if "raw_arguments" in c else json.dumps(c.get("arguments", {}))
            try:
                args = json.loads(raw)
                err = None if isinstance(args, dict) else "arguments must be a JSON object"
                args = args if isinstance(args, dict) else None
            except json.JSONDecodeError as e:
                args, err = None, f"arguments are not valid JSON ({e.msg} at char {e.pos})"
            calls.append(ToolCall(id=c.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=c["name"],
                                  arguments=args, raw_arguments=raw, parse_error=err))
        u = spec.get("usage") or {}
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        usage = Usage(
            input_tokens=int(u.get("input_tokens", chars // 4)),
            output_tokens=int(u.get("output_tokens", 20)),
            known=True,
            raw={"fake": True, **u},
        )
        return ModelTurn(
            text=spec.get("text", ""),
            tool_calls=calls,
            finish_reason=spec.get("finish_reason", "tool_calls" if calls else "stop"),
            usage=usage,
        )
