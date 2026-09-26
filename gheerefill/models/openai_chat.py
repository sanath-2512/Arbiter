"""OpenAI-compatible Chat Completions adapter (native function calling).

`base_url` is the API prefix to which `/chat/completions` is appended
(e.g. https://api.openai.com/v1, http://localhost:8000/v1).

Usage semantics (OpenAI): prompt_tokens INCLUDES cached tokens
(prompt_tokens_details.cached_tokens); completion_tokens INCLUDES reasoning tokens.
Normalised: input_tokens = prompt_tokens - cached_tokens, cache_read_tokens = cached_tokens.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from gheerefill.config import ModelConfig
from gheerefill.models.base import ErrorClass, ModelError, ModelTurn, ToolCall, ToolSpec, Usage
from gheerefill.models.http import post_json, post_sse


def normalize_openai_usage(usage: Any) -> Usage:
    if not isinstance(usage, dict):
        return Usage(known=False, raw={})
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    pdet = usage.get("prompt_tokens_details") or {}
    cdet = usage.get("completion_tokens_details") or {}
    cached = int((pdet.get("cached_tokens") if isinstance(pdet, dict) else 0) or 0)
    reasoning = int((cdet.get("reasoning_tokens") if isinstance(cdet, dict) else 0) or 0)
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=completion,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        reasoning_tokens=reasoning,
        known="prompt_tokens" in usage or "completion_tokens" in usage,
        raw=usage,
    )


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "output_text"):
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(content)


class OpenAIChatClient:
    provider = "openai_chat"

    def __init__(self, cfg: ModelConfig, api_key: str):
        self.cfg = cfg
        self.model_name = cfg.name
        self._api_key = api_key

    def render_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                out.append({"role": self.cfg.system_role, "content": m["content"]})
            elif role == "user":
                out.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                calls = m.get("tool_calls") or []
                d: dict[str, Any] = {"role": "assistant", "content": m.get("content") or (None if calls else "")}
                if calls:
                    d["tool_calls"] = [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in calls
                    ]
                out.append(d)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                raise ValueError(f"unknown message role {role!r}")
        return out

    def build_body(self, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.cfg.name, "messages": self.render_messages(messages)}
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if self.cfg.max_tokens_field:
            body[self.cfg.max_tokens_field] = self.cfg.max_output_tokens
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        body.update(self.cfg.extra_body)
        return body

    def parse_response(self, data: dict[str, Any]) -> ModelTurn:
        usage = normalize_openai_usage(data.get("usage"))
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError(ErrorClass.BAD_RESPONSE, "response has no choices", usage_uncertain=not usage.known)
        choice = choices[0] or {}
        msg = choice.get("message") or {}
        notes: list[str] = []
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "")
            raw = fn.get("arguments")
            if isinstance(raw, dict):
                raw = json.dumps(raw)
                notes.append("tool call arguments arrived as an object; serialised to JSON")
            raw = raw if isinstance(raw, str) else ""
            args: dict[str, Any] | None = None
            err = None
            try:
                parsed = json.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    err = f"arguments must be a JSON object, got {type(parsed).__name__}"
            except json.JSONDecodeError as e:
                err = f"arguments are not valid JSON ({e.msg} at char {e.pos})"
            call_id = tc.get("id") if isinstance(tc, dict) else None
            if not call_id:
                call_id = f"call_{uuid.uuid4().hex[:12]}"
                notes.append("provider omitted a tool call id; generated one")
            calls.append(ToolCall(id=str(call_id), name=name, arguments=args, raw_arguments=raw, parse_error=err))
        return ModelTurn(
            text=_content_text(msg.get("content")),
            tool_calls=calls,
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=usage,
            notes=notes,
        )

    def adapt(self, error: ModelError) -> str | None:
        """One-step parameter compatibility repair driven by the provider's own error message.
        Only request *parameters* change; the model, provider and tool protocol never do."""
        msg = error.message.lower()
        if "max_tokens" in msg and self.cfg.max_tokens_field == "max_tokens":
            self.cfg.max_tokens_field = "max_completion_tokens"
            return "provider rejected 'max_tokens'; now sending 'max_completion_tokens'"
        if "max_completion_tokens" in msg and self.cfg.max_tokens_field == "max_completion_tokens":
            self.cfg.max_tokens_field = "max_tokens"
            return "provider rejected 'max_completion_tokens'; now sending 'max_tokens'"
        if "temperature" in msg and self.cfg.temperature is not None:
            self.cfg.temperature = None
            return "provider rejected 'temperature'; now using the provider default"
        if "stream_options" in msg and self.cfg.stream_usage:
            self.cfg.stream_usage = False
            return "provider rejected 'stream_options'; usage may be unreported in streaming mode"
        return None

    def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, timeout_s: float) -> ModelTurn:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", **self.cfg.extra_headers}
        body = self.build_body(messages, tools)
        if not self.cfg.stream:
            return self.parse_response(post_json(url, headers, body, timeout_s))
        body["stream"] = True
        if self.cfg.stream_usage:
            body["stream_options"] = {"include_usage": True}
        return self.parse_response(accumulate_openai_stream(post_sse(url, headers, body, timeout_s)))


def accumulate_openai_stream(events) -> dict[str, Any]:
    """Rebuild a non-streaming chat.completion dict from chat.completion.chunk events."""
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish = ""
    usage = None
    for kind, obj in events:
        if kind == "json":
            return obj
        if kind == "done":
            break
        if obj.get("usage"):
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index", len(calls))), {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if calls:
        message["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for _, c in sorted(calls.items())
        ]
    out: dict[str, Any] = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage is not None:
        out["usage"] = usage
    return out
