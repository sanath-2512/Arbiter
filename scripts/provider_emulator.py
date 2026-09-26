#!/usr/bin/env python3
"""Provider emulators for DeepSeek and Qwen (DashScope) behaviour (dev/test only; NOT a model).

An OpenAI-compatible /chat/completions endpoint whose *decisions* come from a policy
(`respond(messages, tools) -> {"content", "tool_calls"}`, e.g. a scripted policy) and whose *wire
behaviour* follows a provider family, as documented by the provider or reported against real
deployments (sources in NOTES.md):

deepseek (V4 / V4.1, thinking mode on by default)
  requests  - `reasoning_content` must be passed back on assistant turns that made tool calls
              (400 "The `reasoning_content` in the thinking mode must be passed back to the API.")
            - an assistant message with tool calls and content "" is rejected (content must be null)
            - max_tokens must be within [1, 393216]
  responses - `reasoning_content` on every turn; blank keep-alive lines before a non-streamed body
            - tool calls leaked into `content` as DSML (V4 and V4.1 spellings), finish_reason "stop"
            - finish_reason "insufficient_system_resource"; 503 "Server overloaded"
            - usage with prompt_cache_hit_tokens / prompt_cache_miss_tokens
qwen (DashScope compatible mode, and open-weight Qwen behind vLLM/SGLang/Ollama)
  requests  - moderation: 400 data_inspection_failed when a message contains the trigger text
            - "Range of input length should be [1, N]" above the input limit
            - optionally stream-only ("only support stream mode")
  responses - tool calls leaked as Qwen3-Coder XML or Hermes JSON; inline <think> blocks
            - arguments as Python literals / trailing commas / objects; missing tool call ids
            - trained tool vocabularies: `str_replace_editor`, `execute_bash`, `file_path`, ...

Quirks are injected at seeded rates, so a run is reproducible. Every injected quirk is counted.
"""

from __future__ import annotations

import json
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

DEEPSEEK_PASSBACK = "The `reasoning_content` in the thinking mode must be passed back to the API."


def _dsml(calls: list[dict], spaced: bool) -> str:
    sp = " " if spaced else ""
    out = [f"<｜DSML｜{sp}{'calls' if spaced else 'tool_calls'}>"]
    for c in calls:
        out.append(f'<｜DSML｜{sp}invoke name="{c["name"]}">')
        for k, v in c["arguments"].items():
            is_str = isinstance(v, str)
            out.append(f'<｜DSML｜{sp}parameter name="{k}" string="{"true" if is_str else "false"}">'
                       f'{v if is_str else json.dumps(v)}</｜DSML｜{sp}parameter>')
        out.append(f"</｜DSML｜{sp}invoke>")
    out.append(f"</｜DSML｜{sp}{'calls' if spaced else 'tool_calls'}>")
    return "\n".join(out)


def _qwen_xml(calls: list[dict]) -> str:
    parts = []
    for c in calls:
        params = "".join(f"<parameter={k}>\n{v if isinstance(v, str) else json.dumps(v)}\n</parameter>\n"
                         for k, v in c["arguments"].items())
        parts.append(f"<tool_call>\n<function={c['name']}>\n{params}</function>\n</tool_call>")
    return "\n".join(parts)


def _hermes(calls: list[dict]) -> str:
    return "\n".join(f"<tool_call>\n{json.dumps({'name': c['name'], 'arguments': c['arguments']})}\n</tool_call>"
                     for c in calls)


VOCAB = {  # a trained vocabulary a Qwen model may fall back to
    "bash": lambda a: ("execute_bash", {"command": a.get("command", "")}),
    "edit_file": lambda a: ("str_replace_editor", {"command": "str_replace", "path": a.get("path"),
                                                   "old_str": a.get("old_str"), "new_str": a.get("new_str")}),
    "read_file": lambda a: ("str_replace_editor", {"command": "view", "path": a.get("path")}),
    "write_file": lambda a: ("str_replace_editor", {"command": "create", "path": a.get("path"),
                                                    "file_text": a.get("content")}),
    "submit": lambda a: ("finish", a),
}
ARG_RENAMES = {"path": "file_path", "old_str": "old_string", "new_str": "new_string", "command": "cmd"}

FAMILIES = {
    "deepseek": {"thinking": True, "leaks": ("dsml", "dsml41"), "passback": True, "null_content": True,
                 "keepalive": True, "max_tokens": 393216, "input_limit": 0, "moderation": None, "stream_only": False},
    "qwen": {"thinking": False, "leaks": ("qwen_xml", "hermes"), "passback": False, "null_content": False,
             "keepalive": False, "max_tokens": 65536, "input_limit": 0, "moderation": "MODERATION-TRIGGER",
             "stream_only": False},
}
QUIRKS = {  # name: default rate
    "deepseek": {"leak": 0.3, "insufficient_resource": 0.05, "overloaded": 0.05, "no_id": 0.1},
    "qwen": {"leak": 0.3, "think_inline": 0.3, "python_args": 0.15, "trailing_comma": 0.1, "object_args": 0.1,
             "no_id": 0.2, "vocab": 0.2, "arg_rename": 0.15, "prefix_name": 0.1},
}


class ProviderEmulator:
    def __init__(self, respond: Callable[[list, list], dict], family: str, *, seed: int = 0, scale: float = 1.0,
                 overrides: dict[str, Any] | None = None, rates: dict[str, float] | None = None):
        self.respond, self.family = respond, family
        self.cfg = {**FAMILIES[family], **(overrides or {})}
        self.rates = {k: min(1.0, v * scale) for k, v in {**QUIRKS[family], **(rates or {})}.items()}
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.requests = 0
        self.rejections: dict[str, int] = {}
        self.injected: dict[str, int] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in outer.models()]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with outer.lock:
                    status, payload = outer.handle(body)
                if status == 200 and body.get("stream"):
                    return self._stream(payload)
                self._send(status, payload, keepalive=status == 200 and outer.cfg["keepalive"])

            def _send(self, status, payload, keepalive=False):
                data = (b"\n\n" if keepalive else b"") + json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, payload):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                msg = payload["choices"][0]["message"]
                base = {"id": payload["id"], "object": "chat.completion.chunk", "model": payload["model"]}

                def send(obj):
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                    self.wfile.flush()

                if outer.cfg["keepalive"]:
                    self.wfile.write(b": keep-alive\n\n")
                if msg.get("reasoning_content"):
                    send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": ""}}]})
                    for i in range(0, len(msg["reasoning_content"]), 40):
                        send({**base, "choices": [{"index": 0, "delta": {"reasoning_content": msg["reasoning_content"][i:i + 40]}}]})
                text = msg.get("content") or ""
                for i in range(0, len(text), 40):
                    send({**base, "choices": [{"index": 0, "delta": {"content": text[i:i + 40]}}]})
                for i, c in enumerate(msg.get("tool_calls") or []):
                    args = c["function"]["arguments"]
                    send({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": i, "id": c.get("id"), "type": "function",
                         "function": {"name": c["function"]["name"], "arguments": args[:len(args) // 2]}}]}}]})
                    send({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": i, "function": {"arguments": args[len(args) // 2:]}}]}}]})
                send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": payload["choices"][0]["finish_reason"]}],
                      "usage": payload.get("usage")})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    # ------------------------------------------------------------------ provider rules
    def models(self) -> list[str]:
        return ["deepseek-v4-pro", "deepseek-flash"] if self.family == "deepseek" else ["qwen3.8-max", "qwen3-coder-plus"]

    def _reject(self, kind: str, status: int, error: dict) -> tuple[int, dict]:
        self.rejections[kind] = self.rejections.get(kind, 0) + 1
        return status, {"error": error}

    def _roll(self, quirk: str) -> bool:
        hit = self.rng.random() < self.rates.get(quirk, 0.0)
        if hit:
            self.injected[quirk] = self.injected.get(quirk, 0) + 1
        return hit

    def handle(self, body: dict) -> tuple[int, dict]:
        self.requests += 1
        msgs = body.get("messages") or []
        cfg = self.cfg
        if cfg["stream_only"] and not body.get("stream"):
            return self._reject("stream_only", 400, {"code": "invalid_parameter_error", "type": "invalid_request_error",
                                                     "message": "This model only support stream mode, please enable the "
                                                                "stream parameter."})
        mt = body.get("max_tokens") or body.get("max_completion_tokens")
        if mt is not None and not (1 <= int(mt) <= cfg["max_tokens"]):
            return self._reject("max_tokens", 400, {"message": f"Invalid max_tokens value, the valid range of max_tokens "
                                                               f"is [1, {cfg['max_tokens']}]", "type": "invalid_request_error"})
        thinking = cfg["thinking"] and (body.get("thinking") or {}).get("type") != "disabled"
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                if cfg["null_content"] and m.get("content") == "":
                    return self._reject("empty_content", 400, {"message": "Invalid assistant message: content must "
                                                                          "not be an empty string when tool_calls are "
                                                                          "present", "type": "invalid_request_error"})
                if cfg["passback"] and thinking and body.get("tools") and not m.get("reasoning_content"):
                    return self._reject("passback", 400, {"message": DEEPSEEK_PASSBACK, "type": "invalid_request_error",
                                                          "code": "invalid_request_error"})
        text = json.dumps(msgs)
        if cfg["moderation"] and cfg["moderation"] in text:
            return self._reject("moderation", 400, {"code": "data_inspection_failed", "param": None,
                                                    "message": "Input data may contain inappropriate content.",
                                                    "type": "data_inspection_failed"})
        if cfg["input_limit"] and len(text) // 3 > cfg["input_limit"]:
            return self._reject("input_length", 400, {"code": "invalid_parameter_error", "message": "<400> "
                                                      "InternalError.Algo.InvalidParameter: Range of input length should be "
                                                      f"[1, {cfg['input_limit']}]", "type": "invalid_request_error"})
        if self.family == "deepseek" and self._roll("overloaded"):
            return 503, {"error": {"message": "Server overloaded, please retry shortly.", "type": "server_error"}}
        decision = self.respond(msgs, body.get("tools") or [])
        return 200, self.render(body, decision, thinking)

    # ------------------------------------------------------------------ provider output quirks
    def render(self, body: dict, decision: dict, thinking: bool) -> dict:
        calls = [{"name": c["name"], "arguments": dict(c.get("arguments") or {})} for c in decision.get("tool_calls") or []]
        content = decision.get("content") or ""
        reasoning = f"Reasoning about the next step: {content[:80]} ..." if thinking else ""
        finish = "tool_calls" if calls else "stop"
        wire_calls: list[dict] | None = None
        if self.family == "deepseek":
            if calls and self._roll("leak"):
                content = (content + "\n" if content else "") + _dsml(calls, spaced=self.rng.random() < 0.5)
                calls, finish = [], "stop"
            elif not calls and self._roll("insufficient_resource"):
                finish = "insufficient_system_resource"
        else:
            if calls and self._roll("leak"):
                content = (content + "\n" if content else "") + (_qwen_xml(calls) if self.rng.random() < 0.6 else _hermes(calls))
                calls, finish = [], "stop"
            if self._roll("think_inline"):
                content = f"<think>\nLet me think about: {content[:60]}\n</think>\n\n{content}"
        if calls:
            wire_calls = []
            for i, c in enumerate(calls):
                name, args = c["name"], c["arguments"]
                if self.family == "qwen":
                    if name in VOCAB and self._roll("vocab"):
                        name, args = VOCAB[name](args)
                    elif self._roll("arg_rename"):
                        args = {ARG_RENAMES.get(k, k): v for k, v in args.items()}
                    if self._roll("prefix_name"):
                        name = "functions." + name
                raw: Any = json.dumps(args)
                if self.family == "qwen":
                    if self._roll("python_args"):
                        raw = repr(args)
                    elif self._roll("trailing_comma") and raw.endswith("}") and len(raw) > 2:
                        raw = raw[:-1] + ",}"
                    elif self._roll("object_args"):
                        raw = args
                cid = "" if self._roll("no_id") else f"call_{self.requests}_{i}"
                wire_calls.append({"id": cid, "type": "function", "function": {"name": name, "arguments": raw}})
        msg: dict[str, Any] = {"role": "assistant", "content": None if wire_calls and not content else content}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if wire_calls:
            msg["tool_calls"] = wire_calls
        prompt = len(json.dumps(body.get("messages") or [])) // 4
        hit = int(prompt * 0.8) if self.requests > 1 else 0
        usage = {"prompt_tokens": prompt, "completion_tokens": 40 + len(reasoning) // 4,
                 "total_tokens": prompt + 40 + len(reasoning) // 4}
        if self.family == "deepseek":
            usage.update(prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=prompt - hit)
        else:
            usage["prompt_tokens_details"] = {"cached_tokens": hit}
        return {"id": f"emu-{self.requests}", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage}

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def stats(self) -> dict[str, Any]:
        return {"family": self.family, "requests": self.requests, "injected": dict(self.injected),
                "rejections": dict(self.rejections)}


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.policy_server import load_policy

    with ProviderEmulator(load_policy(sys.argv[1]), sys.argv[2], seed=int(sys.argv[3]) if len(sys.argv) > 3 else 0) as e:
        print(e.base_url, flush=True)
        threading.Event().wait()
