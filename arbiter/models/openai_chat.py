"""OpenAI-compatible Chat Completions adapter (native function calling).

`base_url` is the API prefix to which `/chat/completions` is appended
(e.g. https://api.openai.com/v1, http://localhost:8000/v1).

Usage semantics (OpenAI): prompt_tokens INCLUDES cached tokens
(prompt_tokens_details.cached_tokens); completion_tokens INCLUDES reasoning tokens.
Normalised: input_tokens = prompt_tokens - cached_tokens, cache_read_tokens = cached_tokens.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from arbiter.config import ModelConfig
from arbiter.models.base import ErrorClass, ModelError, ModelTurn, ToolCall, ToolSpec, Usage, output_token_limit
from arbiter.models.http import post_json, post_sse

# Per-tool-call fields a provider returns and requires back on later requests (Gemini's OpenAI
# compatibility puts the thought signature in `extra_content`).
ECHOED_CALL_FIELDS = ("extra_content",)
SIGNATURE_STUB = {"extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}


def normalize_openai_usage(usage: Any) -> Usage:
    if not isinstance(usage, dict):
        return Usage(known=False, raw={})
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    pdet = usage.get("prompt_tokens_details") or {}
    cdet = usage.get("completion_tokens_details") or {}
    cached = int((pdet.get("cached_tokens") if isinstance(pdet, dict) else 0) or 0)
    if not cached and usage.get("prompt_cache_hit_tokens"):  # DeepSeek: prompt = hit + miss
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
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
        self._stub_signatures = False  # set once the provider asks for thought signatures

    def render_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        passback = self.cfg.reasoning_passback
        for m in messages:
            role = m["role"]
            if role == "system":
                out.append({"role": self.cfg.system_role, "content": m["content"]})
            elif role == "user":
                out.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                calls = m.get("tool_calls") or []
                # null (not "") next to tool calls: DeepSeek rejects an empty string there; an assistant
                # turn with neither text nor calls gets a placeholder instead of an empty message
                d: dict[str, Any] = {"role": "assistant", "content": m.get("content") or (None if calls else "(no reply)")}
                if calls:
                    d["tool_calls"] = [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                         **(c.get("extra") or (SIGNATURE_STUB if self._stub_signatures else {}))}
                        for c in calls
                    ]
                reasoning = m.get("reasoning")
                if reasoning and (passback == "all" or (passback == "auto" and calls)):
                    d["reasoning_content"] = reasoning
                out.append(d)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"] or "(no output)"})
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
            extra = {k: tc[k] for k in ECHOED_CALL_FIELDS if isinstance(tc, dict) and tc.get(k)}
            calls.append(ToolCall(id=str(call_id), name=name, arguments=args, raw_arguments=raw, parse_error=err,
                                  extra=extra or None))
        finish = str(choice.get("finish_reason") or "")
        text = _content_text(msg.get("content"))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if not isinstance(reasoning, str):
            reasoning = json.dumps(reasoning)
        if finish == "insufficient_system_resource" and not calls:
            # DeepSeek ends a response early under load; the partial turn is useless, ask again
            raise ModelError(ErrorClass.SERVER, "provider stopped the response: insufficient_system_resource",
                             usage_uncertain=True)
        return ModelTurn(
            text=text,
            tool_calls=calls,
            finish_reason=finish,
            usage=usage,
            notes=notes,
            reasoning=reasoning,
        )

    def adapt(self, error: ModelError) -> str | None:
        """One-step parameter compatibility repair driven by the provider's own error message.
        Only request *parameters* change; the model, provider and tool protocol never do."""
        msg = error.message.lower()
        limit = output_token_limit(error.message, self.cfg.max_output_tokens)
        if limit:
            self.cfg.max_output_tokens = limit
            return f"provider caps output at {limit} tokens; max_output_tokens lowered to {limit}"
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
        if "reasoning_content" in msg or "content[].thinking" in msg:
            if re.search(r"must be passed back|pass(ed)? back|is required|missing", msg):
                if self.cfg.reasoning_passback != "all":
                    self.cfg.reasoning_passback = "all"
                    return "provider requires reasoning_content on earlier assistant turns; now sent on all of them"
                if self.cfg.extra_body.get("thinking") != {"type": "disabled"}:
                    self.cfg.extra_body["thinking"] = {"type": "disabled"}
                    return "reasoning passback still rejected; thinking mode disabled for the rest of the run"
            elif self.cfg.reasoning_passback != "none":
                self.cfg.reasoning_passback = "none"
                return "provider rejected reasoning_content in messages; no longer sent"
        if "thought_signature" in msg and not self._stub_signatures:
            # calls recorded without a signature (recovered from text, or older turns): Gemini's
            # documented placeholder lets them through; calls that carried one still send their own
            self._stub_signatures = True
            return "provider requires a thought signature on every tool call; calls without one now carry its placeholder"
        if re.search(r"only supports? stream|stream mode|non-stream(ing)? calls?|must be set to false for non-stream", msg) \
                and not self.cfg.stream:
            self.cfg.stream = True
            return "provider requires streaming for this model; now streaming"
        return None

    def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, timeout_s: float,
                 total_s: float | None = None) -> ModelTurn:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", **self.cfg.extra_headers}
        body = self.build_body(messages, tools)
        if not self.cfg.stream:
            return self.parse_response(post_json(url, headers, body, timeout_s, total_s))
        body["stream"] = True
        if self.cfg.stream_usage:
            body["stream_options"] = {"include_usage": True}
        return self.parse_response(accumulate_openai_stream(post_sse(url, headers, body, timeout_s, total_s)))


def accumulate_openai_stream(events) -> dict[str, Any]:
    """Rebuild a non-streaming chat.completion dict from chat.completion.chunk events."""
    content: list[str] = []
    reasoning: list[str] = []
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
            r = delta.get("reasoning_content") if delta.get("reasoning_content") is not None else delta.get("reasoning")
            if isinstance(r, str):
                reasoning.append(r)
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index", len(calls))), {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                for k in ECHOED_CALL_FIELDS:
                    if tc.get(k):
                        slot[k] = tc[k]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]},
             **{k: c[k] for k in ECHOED_CALL_FIELDS if c.get(k)}}
            for _, c in sorted(calls.items())
        ]
    out: dict[str, Any] = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage is not None:
        out["usage"] = usage
    return out
