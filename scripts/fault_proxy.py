#!/usr/bin/env python3
"""Fault-injecting HTTP proxy between gheerefill and a model endpoint (dev only).

Every request is forwarded unchanged to UPSTREAM (a live provider root such as
https://api.anthropic.com, or the policy server), except the POSTs numbered in the schedule, which
get a fault instead:

  429          rate limited, with Retry-After
  500          server overloaded
  output_cap   400 "max_tokens is too large ... supports at most 4096" (output-cap adaptation)
  overflow     400 "maximum context length ..." (context reduction)
  unsupported_param  400 "Unsupported parameter: 'max_tokens' ..." (parameter adaptation)
  auth         401 invalid API key (must stop at once, not retry)
  hang         no answer for N seconds (client timeout)
  disconnect   connection closed without a response
  garbage      200 with a body that is not JSON

gheerefill is pointed at the proxy with AI_BASE_URL (and AI_PROVIDER for Anthropic keys); the key
passes through unchanged and is never logged.

    python scripts/fault_proxy.py UPSTREAM '[{"at": 2, "kind": "429"}, {"at": 4, "kind": "overflow"}]'
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORWARD_HEADERS = ("authorization", "x-api-key", "anthropic-version", "anthropic-beta", "content-type", "accept")
FAULTS = {
    "429": (429, {"error": {"message": "Rate limit reached for requests", "type": "rate_limit_error"}}),
    "500": (500, {"error": {"message": "The server is overloaded", "type": "overloaded_error"}}),
    "output_cap": (400, {"error": {"message": "max_tokens is too large: 32768. This model supports at most 4096 "
                                              "completion tokens, whereas you provided 32768."}}),
    "overflow": (400, {"error": {"message": "This model's maximum context length is 16385 tokens. However, your "
                                            "messages resulted in 40213 tokens. Please reduce the length."}}),
    "unsupported_param": (400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this "
                                                     "model. Use 'max_completion_tokens' instead.",
                                          "type": "invalid_request_error", "param": "max_tokens",
                                          "code": "unsupported_parameter"}}),
    "auth": (401, {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error",
                             "code": "invalid_api_key"}}),
}


class FaultProxy:
    def __init__(self, upstream: str, schedule: list[dict]):
        self.upstream = upstream.rstrip("/")
        self.schedule = {int(f["at"]): f for f in schedule}
        self.posts = 0
        self.injected: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status: int, body: bytes, headers: dict[str, str] | None = None):
                self.send_response(status)
                for k, v in (headers or {"Content-Type": "application/json"}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _forward(self, method: str, body: bytes | None):
                headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS}
                req = urllib.request.Request(outer.upstream + self.path, data=body, headers=headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=600) as r:
                        self._reply(r.status, r.read(), {"Content-Type": r.headers.get("Content-Type", "application/json")})
                except urllib.error.HTTPError as e:
                    self._reply(e.code, e.read(), {"Content-Type": "application/json"})
                except (urllib.error.URLError, OSError) as e:
                    self._reply(502, json.dumps({"error": {"message": f"proxy upstream error: {e}"}}).encode())

            def do_GET(self):
                self._forward("GET", None)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.posts += 1
                fault = outer.schedule.get(outer.posts)
                if fault is None:
                    return self._forward("POST", body)
                outer.injected.append({"post": outer.posts, "kind": fault["kind"], "time": time.time()})
                kind = fault["kind"]
                if kind in FAULTS:
                    status, payload = FAULTS[kind]
                    extra = {"Content-Type": "application/json"}
                    if kind == "429":
                        extra["Retry-After"] = str(fault.get("retry_after", 1))
                    return self._reply(status, json.dumps(payload).encode(), extra)
                if kind == "hang":
                    time.sleep(float(fault.get("seconds", 30)))
                    return self._reply(504, b'{"error": {"message": "gateway timeout"}}')
                if kind == "disconnect":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return None
                if kind == "garbage":
                    return self._reply(200, b"<html>502 Bad Gateway</html>", {"Content-Type": "text/html"})
                return self._forward("POST", body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    with FaultProxy(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else []) as p:
        print(p.base_url, flush=True)
        threading.Event().wait()
