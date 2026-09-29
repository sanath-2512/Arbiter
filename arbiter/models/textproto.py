"""Explicitly delimited text-action protocol, for endpoints without native tool calling.

This is a separate, explicitly selected profile (`model.tool_protocol = "text"`); the
harness never switches protocols on its own. Actions are parsed ONLY from the newest
assistant reply. Tool results are sent back as user messages and are never parsed for
actions, so text inside tool output cannot be executed.

Format (one or more per reply):

    <function=bash>
    <parameter=command>
    ls -la
    </parameter>
    </function>
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import unquote

from arbiter.models.base import ModelClient, ModelTurn, ToolCall, ToolSpec

_FUNC_RE = re.compile(r"<function=([A-Za-z0-9_.\-]+)>(.*?)</function>", re.S)
_PARAM_RE = re.compile(r"<parameter=([A-Za-z0-9_]+)>(.*?)</parameter>", re.S)
_OPEN_RE = re.compile(r"<function=([A-Za-z0-9_.\-]+)>")


def _strip_one_newline(s: str) -> str:
    if s.startswith("\n"):
        s = s[1:]
    if s.endswith("\n"):
        s = s[:-1]
    return s


def _convert(value: str, schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    v = value.strip()
    if t == "integer":
        return int(v)
    if t == "number":
        return float(v)
    if t == "boolean":
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"expected true/false, got {v!r}")
    return value


def parse_actions(text: str, tools: list[ToolSpec]) -> list[ToolCall]:
    specs = {t.name: t for t in tools}
    calls: list[ToolCall] = []
    consumed_end = 0
    for m in _FUNC_RE.finditer(text):
        consumed_end = m.end()
        name, body = m.group(1), m.group(2)
        args: dict[str, Any] = {}
        errors = []
        props = specs[name].parameters.get("properties", {}) if name in specs else {}
        for p in _PARAM_RE.finditer(body):
            key, raw = p.group(1), _strip_one_newline(p.group(2))
            if key in args:
                errors.append(f"parameter {key!r} given twice")
                continue
            try:
                args[key] = _convert(raw, props.get(key, {}))
            except ValueError as e:
                errors.append(f"parameter {key!r}: {e}")
        # Qwen 2.5 Coder on Ollama sometimes serialises a parameter as
        # ``command=echo%20ok\n</command>`` instead of the advertised
        # <parameter=command> block. Parse that explicit, paired form only.
        for p in re.finditer(r"^\s*([A-Za-z0-9_]+)=(.*?)\n</\1>", body, re.M | re.S):
            key, raw = p.group(1), unquote(p.group(2).strip())
            if key in args:
                continue
            try:
                args[key] = _convert(raw, props.get(key, {}))
            except ValueError as e:
                errors.append(f"parameter {key!r}: {e}")
        calls.append(
            ToolCall(
                id=f"t{uuid.uuid4().hex[:10]}",
                name=name,
                arguments=None if errors else args,
                raw_arguments=json.dumps(args),
                parse_error="; ".join(errors) or None,
            )
        )
    tail = text[consumed_end:]
    dangling = _OPEN_RE.search(tail)
    if dangling:
        calls.append(
            ToolCall(
                id=f"t{uuid.uuid4().hex[:10]}",
                name=dangling.group(1),
                arguments=None,
                raw_arguments="",
                parse_error="unterminated <function=...> block (missing </function>); the reply may have been cut off",
            )
        )
    return calls


def tools_prompt(tools: list[ToolSpec]) -> str:
    lines = [
        "# Tool use",
        "Act by writing tool calls in your reply, exactly in this format (you may write several):",
        "",
        "<function=TOOL_NAME>",
        "<parameter=PARAM_NAME>",
        "value (raw text: no quotes, no escaping)",
        "</parameter>",
        "</function>",
        "",
        "Results come back in <tool_result> blocks. Available tools:",
    ]
    for t in tools:
        props = t.parameters.get("properties", {})
        req = set(t.parameters.get("required", []))
        params = ", ".join(f"{k}: {v.get('type', 'string')}{'' if k in req else ' (optional)'}" for k, v in props.items())
        lines.append(f"- {t.name}({params}): {t.description}")
    return "\n".join(lines)


def convert_messages(messages: list[dict[str, Any]], tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Transcript -> plain system/user/assistant messages for a text-protocol request."""
    out: list[dict[str, Any]] = []
    prompt = tools_prompt(tools)
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append({"role": "system", "content": m["content"] + "\n\n" + prompt})
        elif role == "assistant":
            out.append({"role": "assistant", "content": m.get("content") or "(no content)", "tool_calls": []})
        elif role == "tool":
            block = f'<tool_result name="{m.get("name", "")}">\n{m["content"]}\n</tool_result>'
            if out and out[-1]["role"] == "user" and out[-1].get("_tool_results"):
                out[-1]["content"] += "\n" + block
            else:
                out.append({"role": "user", "content": block, "_tool_results": True})
        else:
            out.append({"role": role, "content": m["content"]})
    for m in out:
        m.pop("_tool_results", None)
    return out


class TextProtocolClient:
    """Wraps a provider client: no native `tools` field; actions parsed from reply text."""

    def __init__(self, inner: ModelClient):
        self.inner = inner
        self.provider = inner.provider
        self.model_name = inner.model_name

    def adapt(self, error):
        return self.inner.adapt(error) if hasattr(self.inner, "adapt") else None

    def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, timeout_s: float,
                 total_s: float | None = None) -> ModelTurn:
        turn = self.inner.complete(convert_messages(messages, tools), [], timeout_s=timeout_s, total_s=total_s)
        turn.tool_calls = parse_actions(turn.text, tools)
        return turn
