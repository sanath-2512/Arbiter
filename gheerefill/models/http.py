"""Minimal JSON-over-HTTPS POST using the standard library.

Honours HTTPS_PROXY and the process CA configuration (SSL_CERT_FILE) through urllib.
Every failure is converted to a classified ModelError. The request body and headers are
never logged here (headers carry the credential).
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

from gheerefill.models.base import ErrorClass, ModelError, classify_http_error


def _ssl_context() -> ssl.SSLContext:
    import os

    ctx = ssl.create_default_context()
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
            except (ssl.SSLError, OSError):
                pass
            break
    return ctx


def post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    ctx = _ssl_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise classify_http_error(e.code, err_body, dict(e.headers or {})) from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ModelError(ErrorClass.TIMEOUT, f"request timed out after {timeout_s:.0f}s", usage_uncertain=True) from None
        if isinstance(reason, ConnectionRefusedError):
            # Nothing reached the server: certainly not billed.
            raise ModelError(ErrorClass.NETWORK, f"connection refused: {reason}") from None
        if isinstance(reason, ssl.SSLError):
            raise ModelError(ErrorClass.NETWORK, f"TLS error: {reason}") from None
        raise ModelError(ErrorClass.NETWORK, f"network error: {reason}", usage_uncertain=True) from None
    except (socket.timeout, TimeoutError):
        raise ModelError(ErrorClass.TIMEOUT, f"request timed out after {timeout_s:.0f}s", usage_uncertain=True) from None
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        raise ModelError(ErrorClass.NETWORK, f"connection interrupted: {type(e).__name__}: {e}", usage_uncertain=True) from None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        # A 200 with an unreadable body was probably billed.
        raise ModelError(ErrorClass.BAD_RESPONSE, f"unparseable response body: {e}", usage_uncertain=True) from None
    if not isinstance(parsed, dict):
        raise ModelError(ErrorClass.BAD_RESPONSE, "response is not a JSON object", usage_uncertain=True)
    if "error" in parsed and not parsed.get("choices") and not parsed.get("content"):
        # Some gateways return errors with HTTP 200.
        err = parsed["error"]
        status = err.get("code") if isinstance(err, dict) and isinstance(err.get("code"), int) else 400
        raise classify_http_error(status, json.dumps(parsed), {})
    return parsed
