"""Scripted OpenAI-compatible /chat/completions server (test infrastructure; NOT a model).

Stateless: the reply index is the number of assistant messages already in the request, so any
client (our urllib transport, litellm inside mini-swe-agent) can replay the same script.
Script: {"<tool-set key>": [turn, ...]} where the key is the sorted, comma-joined tool names
offered in the request (or "*" as a fallback); turn = {"content": str,
"tool_calls": [{"name": str, "arguments": {...}}]}.

    python tests/fake_openai_server.py SCRIPT.json   # prints the base URL, serves until killed
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenAIServer:
    def __init__(self, script: dict):
        self.script = script
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                names = sorted(t["function"]["name"] for t in body.get("tools", []))
                turns = outer.script.get(",".join(names)) or outer.script.get("*") or []
                idx = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
                if idx >= len(turns):
                    payload, status = {"error": {"message": "fake script exhausted"}}, 400
                else:
                    t = turns[idx]
                    calls = [{"id": f"call_{idx}_{i}", "type": "function",
                              "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                             for i, c in enumerate(t.get("tool_calls", []))]
                    msg = {"role": "assistant", "content": t.get("content", "")}
                    if calls:
                        msg["tool_calls"] = calls
                    payload, status = {
                        "id": f"fake-{idx}", "object": "chat.completion", "model": body.get("model"),
                        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                        "usage": {"prompt_tokens": 100 + 10 * idx, "completion_tokens": 20, "total_tokens": 120 + 10 * idx},
                    }, 200
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    with open(sys.argv[1]) as fh:
        srv = FakeOpenAIServer(json.load(fh))
    print(srv.base_url, flush=True)
    srv.httpd.serve_forever()
