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


def post_sse(url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float):
    """POST and yield parsed Server-Sent-Event payloads (dicts) until the stream ends.

    `timeout_s` bounds both the gap between reads and the total duration of the stream.
    Any failure after the request was sent is classified with usage_uncertain=True.
    Yields ("done", None) for an OpenAI `[DONE]` sentinel.
    """
    import time as _time

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    ctx = _ssl_context() if url.startswith("https://") else None
    deadline = _time.monotonic() + timeout_s
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s, context=ctx)
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
            raise ModelError(ErrorClass.NETWORK, f"connection refused: {reason}") from None
        raise ModelError(ErrorClass.NETWORK, f"network error: {reason}", usage_uncertain=True) from None
    except (socket.timeout, TimeoutError):
        raise ModelError(ErrorClass.TIMEOUT, f"request timed out after {timeout_s:.0f}s", usage_uncertain=True) from None
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        raise ModelError(ErrorClass.NETWORK, f"connection interrupted: {type(e).__name__}: {e}", usage_uncertain=True) from None
    ended = False
    try:
        with resp:
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:
                # Server ignored `stream`: accept a plain JSON body.
                raw = resp.read()
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise ModelError(ErrorClass.BAD_RESPONSE, f"unparseable response body: {e}", usage_uncertain=True) from None
                yield ("json", parsed)
                ended = True
                return
            event_name = None
            data_lines: list[str] = []
            saw_finish = False
            while True:
                if _time.monotonic() > deadline:
                    raise ModelError(ErrorClass.TIMEOUT, f"stream exceeded {timeout_s:.0f}s", usage_uncertain=True)
                line_b = resp.readline()
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line == "" and data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    if payload.strip() == "[DONE]":
                        ended = True
                        yield ("done", None)
                        return
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        raise ModelError(ErrorClass.BAD_RESPONSE, "malformed stream event", usage_uncertain=True) from None
                    if isinstance(obj, dict) and (event_name == "error" or obj.get("type") == "error" or
                                                  ("error" in obj and "choices" not in obj)):
                        err = obj.get("error", obj)
                        status = 529 if isinstance(err, dict) and err.get("type") == "overloaded_error" else 500
                        if isinstance(err, dict) and isinstance(err.get("code"), int):
                            status = err["code"]
                        raise classify_http_error(status, json.dumps(obj), {})
                    if isinstance(obj, dict) and any((c or {}).get("finish_reason") for c in obj.get("choices") or []):
                        saw_finish = True
                    yield (event_name or "message", obj)
                    event_name = None
                    if isinstance(obj, dict) and obj.get("type") == "message_stop":
                        ended = True
                        return
            if not ended and not saw_finish:
                raise ModelError(ErrorClass.NETWORK, "stream ended before completion", usage_uncertain=True)
    except ModelError:
        raise
    except (socket.timeout, TimeoutError):
        raise ModelError(ErrorClass.TIMEOUT, "stream read timed out", usage_uncertain=True) from None
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        raise ModelError(ErrorClass.NETWORK, f"stream interrupted: {type(e).__name__}: {e}", usage_uncertain=True) from None
