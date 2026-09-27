#!/usr/bin/env python3
"""Local stand-in for the GitHub REST API (dev only). Serves one issue so the official
`make run ISSUE=https://github.com/OWNER/REPO/issues/N` flow can be rehearsed with no network:
point ARBITER_GITHUB_API at the printed URL and ARBITER_GITHUB_CLONE_BASE at a local directory
that holds OWNER/REPO as a git repository.

    python scripts/fake_github.py OWNER/REPO NUMBER ISSUE_FILE     # first line = title, rest = body
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(slug: str, number: int, title: str, body: str) -> ThreadingHTTPServer:
    issue = {"number": number, "title": title, "body": body, "state": "open", "comments": 0,
             "created_at": "2026-09-01T00:00:00Z", "closed_at": None, "user": {"login": "reporter"},
             "html_url": f"https://github.com/{slug}/issues/{number}", "labels": [{"name": "bug"}]}
    routes = {f"/repos/{slug}/issues/{number}": issue, f"/repos/{slug}/issues/{number}/comments": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            payload = routes.get(self.path.split("?", 1)[0])
            data = json.dumps(payload if payload is not None else {"message": "Not Found"}).encode()
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    text = Path(sys.argv[3]).read_text().strip()
    title, _, body = text.partition("\n")
    srv = serve(sys.argv[1], int(sys.argv[2]), title.strip(), body.strip())
    print(f"http://127.0.0.1:{srv.server_address[1]}", flush=True)
    threading.Event().wait()
