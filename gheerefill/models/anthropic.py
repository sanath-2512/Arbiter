"""Anthropic Messages API adapter (native tool use).

`base_url` e.g. https://api.anthropic.com (or .../v1); `/v1/messages` is appended.

Usage semantics (Anthropic): input_tokens EXCLUDES cache reads and cache writes, which
are reported separately (cache_read_input_tokens, cache_creation_input_tokens).

Assistant content blocks (including thinking blocks and their signatures) are stored in
`provider_raw` and replayed verbatim, as required for tool use with extended thinking.
"""

from __future__ import annotations

import json
from typing import Any

from gheerefill.config import ModelConfig
from gheerefill.models.base import ErrorClass, ModelError, ModelTurn, ToolCall, ToolSpec, Usage
from gheerefill.models.http import post_json, post_sse


def normalize_anthropic_usage(usage: Any) -> Usage:
    if not isinstance(usage, dict):
        return Usage(known=False, raw={})
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        known="input_tokens" in usage or "output_tokens" in usage,
        raw=usage,
    )


class AnthropicClient:
    provider = "anthropic_messages"

    def __init__(self, cfg: ModelConfig, api_key: str):
        self.cfg = cfg
        self.model_name = cfg.name
        self._api_key = api_key

    def render(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        system_blocks: list[dict[str, Any]] = []
        out: list[dict[str, Any]] = []

        def push(role: str, blocks: list[dict[str, Any]]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m["role"]
            if role == "system":
                if m["content"]:
                    system_blocks.append({"type": "text", "text": m["content"]})
            elif role == "user":
                if m["content"]:
                    push("user", [{"type": "text", "text": m["content"]}])
            elif role == "tool":
                push(
                    "user",
                    [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"] or "(empty)"}],
                )
            elif role == "assistant":
                raw = m.get("provider_raw")
                if isinstance(raw, list) and raw:
                    push("assistant", [dict(b) for b in raw])
                    continue
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    try:
                        inp = json.loads(c["arguments"]) if c["arguments"].strip() else {}
                    except json.JSONDecodeError:
                        inp = {}
                    if not isinstance(inp, dict):
                        inp = {}
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": inp})
                if not blocks:
                    blocks.append({"type": "text", "text": "(no content)"})
                push("assistant", blocks)
            else:
                raise ValueError(f"unknown message role {role!r}")
        if self.cfg.prompt_cache:
            if system_blocks:
                system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
            if out and out[-1]["role"] == "user" and out[-1]["content"]:
                out[-1]["content"][-1] = dict(out[-1]["content"][-1], cache_control={"type": "ephemeral"})
        return system_blocks, out

    def build_body(self, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> dict[str, Any]:
        system_blocks, msgs = self.render(messages)
        body: dict[str, Any] = {"model": self.cfg.name, "max_tokens": self.cfg.max_output_tokens, "messages": msgs}
        if system_blocks:
            body["system"] = system_blocks
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        body.update(self.cfg.extra_body)
        return body

    def parse_response(self, data: dict[str, Any]) -> ModelTurn:
        usage = normalize_anthropic_usage(data.get("usage"))
        content = data.get("content")
        if not isinstance(content, list):
            raise ModelError(ErrorClass.BAD_RESPONSE, "response has no content list", usage_uncertain=not usage.known)
        texts, calls = [], []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                texts.append(str(b.get("text", "")))
            elif b.get("type") == "tool_use":
                inp = b.get("input")
                args = inp if isinstance(inp, dict) else None
                calls.append(
                    ToolCall(
                        id=str(b.get("id") or ""),
                        name=str(b.get("name") or ""),
                        arguments=args,
                        raw_arguments=json.dumps(inp if inp is not None else {}),
                        parse_error=None if args is not None else "tool input is not an object",
                    )
                )
        return ModelTurn(
            text="".join(texts),
            tool_calls=calls,
            finish_reason=str(data.get("stop_reason") or ""),
            usage=usage,
            provider_raw=content,
        )

    def adapt(self, error: ModelError) -> str | None:
        if "temperature" in error.message.lower() and self.cfg.temperature is not None:
            self.cfg.temperature = None
            return "provider rejected 'temperature'; now using the provider default"
        return None

    def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, timeout_s: float) -> ModelTurn:
        base = self.cfg.base_url.rstrip("/")
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {"x-api-key": self._api_key, "anthropic-version": self.cfg.anthropic_version, **self.cfg.extra_headers}
        body = self.build_body(messages, tools)
        if not self.cfg.stream:
            return self.parse_response(post_json(url, headers, body, timeout_s))
        body["stream"] = True
        return self.parse_response(accumulate_anthropic_stream(post_sse(url, headers, body, timeout_s)))


def accumulate_anthropic_stream(events) -> dict[str, Any]:
    """Rebuild a Messages API response (content blocks incl. thinking signatures) from SSE events."""
    blocks: dict[int, dict[str, Any]] = {}
    partial_json: dict[int, list[str]] = {}
    usage: dict[str, Any] = {}
    stop_reason = ""
    for kind, obj in events:
        if kind == "json":
            return obj
        t = obj.get("type")
        if t == "message_start":
            usage.update((obj.get("message") or {}).get("usage") or {})
        elif t == "content_block_start":
            blocks[obj["index"]] = dict(obj.get("content_block") or {})
            if blocks[obj["index"]].get("type") == "tool_use":
                partial_json[obj["index"]] = []
        elif t == "content_block_delta":
            b = blocks.setdefault(obj["index"], {"type": "text", "text": ""})
            d = obj.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "input_json_delta":
                partial_json.setdefault(obj["index"], []).append(d.get("partial_json", ""))
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = b.get("signature", "") + d.get("signature", "")
        elif t == "message_delta":
            stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason
            usage.update(obj.get("usage") or {})
    content = []
    for i in sorted(blocks):
        b = blocks[i]
        if b.get("type") == "tool_use":
            raw = "".join(partial_json.get(i, []))
            try:
                b["input"] = json.loads(raw) if raw.strip() else (b.get("input") or {})
            except json.JSONDecodeError:
                b["input"] = None  # surfaces as a parse error for this tool call
        content.append(b)
    return {"content": content, "stop_reason": stop_reason, "usage": usage}
