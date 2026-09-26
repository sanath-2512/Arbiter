#!/usr/bin/env python3
"""Scripted-policy model server for mechanism rehearsals (dev only; NOT a model).

Serves an OpenAI-compatible /chat/completions endpoint whose replies come from a Python policy
(`rehearsal/policies/<name>.py`, function `respond(messages, tools) -> {"content", "tool_calls"}`).
gheerefill talks to it through its real HTTP transport (AI_BASE_URL), so a rehearsal exercises the
same code path as a live run. Policies know the reference fix: they exist to drive the harness into
a specific failure mode (a stubborn loop, a late regression, a wrong reproduction) and are labelled
"scripted" in every record. They never measure model capability.

    python scripts/policy_server.py POLICY_NAME     # prints the base URL, serves until killed
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "rehearsal" / "policies"


def load_policy(name: str):
    spec = importlib.util.spec_from_file_location(f"policy_{name}", POLICIES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.respond


class PolicyServer:
    def __init__(self, respond):
        self.respond, self.requests = respond, 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # /models
                body = json.dumps({"data": [{"id": "scripted-policy"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests += 1
                try:
                    reply = outer.respond(req.get("messages", []), req.get("tools", []))
                except Exception as e:  # noqa: BLE001 - surfaces as a provider error to the harness
                    reply = {"error": f"policy error: {type(e).__name__}: {e}"}
                if "error" in reply:
                    body, status = json.dumps({"error": {"message": reply["error"]}}).encode(), 400
                else:
                    idx = outer.requests
                    calls = [{"id": f"call_{idx}_{i}", "type": "function",
                              "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                             for i, c in enumerate(reply.get("tool_calls", []))]
                    msg = {"role": "assistant", "content": reply.get("content", "")}
                    if calls:
                        msg["tool_calls"] = calls
                    chars = sum(len(str(m.get("content") or "")) for m in req.get("messages", []))
                    body, status = json.dumps({
                        "id": f"policy-{idx}", "object": "chat.completion", "model": req.get("model"),
                        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                        "usage": {"prompt_tokens": chars // 4, "completion_tokens": 30, "total_tokens": chars // 4 + 30},
                    }).encode(), 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    with PolicyServer(load_policy(sys.argv[1])) as srv:
        print(srv.base_url, flush=True)
        threading.Event().wait()
