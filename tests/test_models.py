import json
import os
import random
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from gheerefill.config import ModelConfig
from gheerefill.models import make_client, read_api_key
from gheerefill.models.anthropic import AnthropicClient, normalize_anthropic_usage
from gheerefill.models.base import (
    ErrorClass,
    ModelError,
    ToolSpec,
    call_with_retry,
    classify_http_error,
    parse_retry_after,
)
from gheerefill.models.openai_chat import OpenAIChatClient, normalize_openai_usage
from gheerefill.config import ConfigError

TOOL = ToolSpec("bash", "run", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def ok_openai(tool_args='{"command": "ls"}', usage=True):
    body = {
        "choices": [{"message": {"role": "assistant", "content": "thinking",
                                 "tool_calls": [{"id": "c1", "type": "function",
                                                 "function": {"name": "bash", "arguments": tool_args}}]},
                     "finish_reason": "tool_calls"}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 100, "completion_tokens": 20,
                         "prompt_tokens_details": {"cached_tokens": 60},
                         "completion_tokens_details": {"reasoning_tokens": 5}}
    return 200, {}, json.dumps(body)


class ScriptedServer:
    """Local HTTP server returning scripted (status, headers, body) or special actions."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": json.loads(self.rfile.read(n) or b"{}")})
                item = outer.script.pop(0)
                if item == "drop":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"choi")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if isinstance(item, tuple) and item[0] == "sse":
                    _, events, gap = item
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        for ev in events:
                            if ev == "CUT":
                                self.close_connection = True
                                return
                            name, payload = ev if isinstance(ev, tuple) else (None, ev)
                            if name:
                                self.wfile.write(f"event: {name}\n".encode())
                            data = payload if isinstance(payload, str) else json.dumps(payload)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                            time.sleep(gap)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if isinstance(item, tuple) and item[0] == "sleep":
                    time.sleep(item[1])
                    item = item[2]
                status, headers, body = item
                data = body.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


NO_PROXY_ENV = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}


def retry_call(client, *, time_left=lambda: 1000.0, max_attempts=4, sleeps=None, attempts=None):
    sleeps = [] if sleeps is None else sleeps
    attempts = [] if attempts is None else attempts
    return call_with_retry(client, MSGS, [TOOL], max_attempts=max_attempts, base_delay_s=0.01, max_delay_s=0.05,
                           time_left=time_left, request_timeout_s=5, on_attempt=attempts.append,
                           rng=random.Random(0), sleep=sleeps.append, min_call_s=0.1)


class ClassificationTest(unittest.TestCase):
    def test_http_error_classes(self):
        cases = [
            (401, '{"error":{"message":"bad key"}}', ErrorClass.AUTH),
            (403, "forbidden", ErrorClass.AUTH),
            (429, '{"error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}', ErrorClass.QUOTA),
            (402, "payment required", ErrorClass.QUOTA),
            (429, '{"error":{"message":"Rate limit reached"}}', ErrorClass.RATE_LIMIT),
            (400, '{"error":{"message":"This model\'s maximum context length is 8192 tokens"}}', ErrorClass.CONTEXT_OVERFLOW),
            (400, '{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 210000 tokens > 200000 maximum"}}', ErrorClass.CONTEXT_OVERFLOW),
            (400, '{"error":{"message":"Unsupported parameter: \'max_tokens\' is not supported with this model."}}', ErrorClass.UNSUPPORTED),
            (400, '{"error":{"message":"messages: field required"}}', ErrorClass.MALFORMED_REQUEST),
            (404, "model not found", ErrorClass.MALFORMED_REQUEST),
            (500, "oops", ErrorClass.SERVER),
            (529, '{"type":"error","error":{"type":"overloaded_error"}}', ErrorClass.SERVER),
            (408, "timeout", ErrorClass.TIMEOUT),
        ]
        for status, body, cls in cases:
            self.assertEqual(classify_http_error(status, body).cls, cls, (status, body))
        self.assertTrue(classify_http_error(503, "x").usage_uncertain)
        self.assertFalse(classify_http_error(401, "x").usage_uncertain)

    def test_retry_after_parsing(self):
        self.assertEqual(parse_retry_after({"Retry-After": "7"}), 7.0)
        self.assertEqual(parse_retry_after({"retry-after-ms": "1500"}), 1.5)
        self.assertAlmostEqual(parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"},
                                                 now=1445412480.0), 10.0)
        self.assertIsNone(parse_retry_after({}))

    def test_usage_normalisation_does_not_double_count(self):
        u = normalize_openai_usage({"prompt_tokens": 100, "completion_tokens": 20,
                                    "prompt_tokens_details": {"cached_tokens": 60}})
        self.assertEqual((u.input_tokens, u.cache_read_tokens, u.prompt_tokens, u.total_tokens), (40, 60, 100, 120))
        a = normalize_anthropic_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90,
                                       "cache_creation_input_tokens": 30})
        self.assertEqual((a.input_tokens, a.cache_read_tokens, a.cache_write_tokens, a.prompt_tokens), (10, 90, 30, 130))
        self.assertFalse(normalize_openai_usage(None).known)


class ClientParsingTest(unittest.TestCase):
    def cfg(self, **kw):
        c = ModelConfig(name="m", base_url="http://x/v1")
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_openai_malformed_arguments_become_parse_errors(self):
        client = OpenAIChatClient(self.cfg(), "k")
        _, _, body = ok_openai('{"command": "ls"')
        turn = client.parse_response(json.loads(body))
        self.assertIsNotNone(turn.tool_calls[0].parse_error)
        self.assertIsNone(turn.tool_calls[0].arguments)
        _, _, body = ok_openai('["ls"]')
        self.assertIn("JSON object", client.parse_response(json.loads(body)).tool_calls[0].parse_error)

    def test_openai_missing_choices_is_bad_response(self):
        with self.assertRaises(ModelError) as cm:
            OpenAIChatClient(self.cfg(), "k").parse_response({"usage": {"prompt_tokens": 1}})
        self.assertEqual(cm.exception.cls, ErrorClass.BAD_RESPONSE)

    def test_openai_body_settings(self):
        client = OpenAIChatClient(self.cfg(max_tokens_field="max_completion_tokens", extra_body={"reasoning_effort": "high"},
                                          temperature=0.2), "k")
        body = client.build_body(MSGS + [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "bash", "arguments": "{}"}]},
            {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "out", "output_id": "o1"},
        ], [TOOL])
        self.assertEqual(body["max_completion_tokens"], 8192)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["temperature"], 0.2)
        self.assertIsNone(body["messages"][2]["content"])
        self.assertEqual(body["messages"][3], {"role": "tool", "tool_call_id": "c1", "content": "out"})

    def test_anthropic_render_merges_and_replays_thinking(self):
        client = AnthropicClient(self.cfg(prompt_cache=True), "k")
        raw = [{"type": "thinking", "thinking": "hmm", "signature": "sig"},
               {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}},
               {"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "pwd"}}]
        msgs = MSGS + [
            {"role": "assistant", "content": "", "tool_calls": [], "provider_raw": raw},
            {"role": "tool", "tool_call_id": "t1", "name": "bash", "content": "a"},
            {"role": "tool", "tool_call_id": "t2", "name": "bash", "content": ""},
            {"role": "user", "content": "Budget notice"},
        ]
        body = client.build_body(msgs, [TOOL])
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(body["messages"][1]["content"], raw)
        last = body["messages"][2]["content"]
        self.assertEqual([b["type"] for b in last], ["tool_result", "tool_result", "text"])
        self.assertEqual(last[1]["content"], "(empty)")
        self.assertIn("cache_control", last[-1])
        turn = client.parse_response({"content": raw + [{"type": "text", "text": "x"}], "stop_reason": "tool_use",
                                      "usage": {"input_tokens": 3, "output_tokens": 4}})
        self.assertEqual([c.id for c in turn.tool_calls], ["t1", "t2"])
        self.assertEqual(turn.provider_raw[0]["signature"], "sig")

    def test_missing_credential_is_clear(self):
        with self.assertRaises(ConfigError) as cm:
            read_api_key(self.cfg(), env={})
        self.assertIn("AI_API_KEY", str(cm.exception))
        with self.assertRaises(ConfigError):
            make_client(self.cfg(provider="openai_chat"), env={"AI_API_KEY": "  "})


@mock.patch.dict(os.environ, NO_PROXY_ENV)
class TransportTest(unittest.TestCase):
    def client(self, url, provider="openai"):
        cfg = ModelConfig(name="m", base_url=url + "/v1", request_timeout_s=5)
        return OpenAIChatClient(cfg, "sk-test-key-000") if provider == "openai" else AnthropicClient(cfg, "sk-test-key-000")

    def test_success_request_shape(self):
        with ScriptedServer([ok_openai()]) as srv:
            turn = retry_call(self.client(srv.url))
        req = srv.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer sk-test-key-000")
        self.assertEqual(req["body"]["model"], "m")
        self.assertEqual(req["body"]["tools"][0]["function"]["name"], "bash")
        self.assertEqual(turn.tool_calls[0].arguments, {"command": "ls"})
        self.assertEqual(turn.usage.cache_read_tokens, 60)

    def test_auth_failure_is_not_retried(self):
        attempts = []
        with ScriptedServer([(401, {}, '{"error":{"message":"invalid api key"}}')] * 3) as srv:
            with self.assertRaises(ModelError) as cm:
                retry_call(self.client(srv.url), attempts=attempts)
        self.assertEqual(cm.exception.cls, ErrorClass.AUTH)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(srv.requests), 1)

    def test_rate_limit_honours_retry_after_then_succeeds(self):
        sleeps, attempts = [], []
        with ScriptedServer([(429, {"Retry-After": "0.2"}, '{"error":{"message":"slow down"}}'), ok_openai()]) as srv:
            turn = retry_call(self.client(srv.url), sleeps=sleeps, attempts=attempts)
        self.assertEqual(turn.tool_calls[0].name, "bash")
        self.assertEqual([a.outcome for a in attempts], ["rate_limited", "ok"])
        self.assertGreaterEqual(sleeps[0], 0.2)

    def test_retry_hint_beyond_deadline_gives_up(self):
        with ScriptedServer([(429, {"Retry-After": "120"}, "{}")]) as srv:
            with self.assertRaises(ModelError) as cm:
                retry_call(self.client(srv.url), time_left=lambda: 30.0)
        self.assertEqual(cm.exception.cls, ErrorClass.DEADLINE)

    def test_server_errors_bounded(self):
        attempts = []
        with ScriptedServer([(500, {}, "boom")] * 3) as srv:
            with self.assertRaises(ModelError) as cm:
                retry_call(self.client(srv.url), max_attempts=3, attempts=attempts)
        self.assertEqual(cm.exception.cls, ErrorClass.SERVER)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(a.usage_uncertain for a in attempts))

    def test_unsupported_and_context_overflow_are_permanent(self):
        for body, cls in [('{"error":{"message":"Unrecognized request argument supplied: foo"}}', ErrorClass.UNSUPPORTED),
                          ('{"error":{"message":"maximum context length exceeded"}}', ErrorClass.CONTEXT_OVERFLOW)]:
            with ScriptedServer([(400, {}, body)]) as srv:
                with self.assertRaises(ModelError) as cm:
                    retry_call(self.client(srv.url))
            self.assertEqual(cm.exception.cls, cls)
            self.assertEqual(len(srv.requests), 1)

    def test_interrupted_response_marks_usage_uncertain_and_retries(self):
        attempts = []
        with ScriptedServer(["drop", (200, {}, "not json"), ok_openai()]) as srv:
            turn = retry_call(self.client(srv.url), attempts=attempts)
        self.assertEqual([a.outcome for a in attempts], ["network_error", "bad_response", "ok"])
        self.assertTrue(attempts[0].usage_uncertain and attempts[1].usage_uncertain)
        self.assertEqual(turn.tool_calls[0].name, "bash")

    def test_timeout(self):
        with ScriptedServer([("sleep", 1.5, ok_openai())]) as srv:
            cfg = ModelConfig(name="m", base_url=srv.url + "/v1")
            with self.assertRaises(ModelError) as cm:
                OpenAIChatClient(cfg, "k").complete(MSGS, [TOOL], timeout_s=0.3)
        self.assertEqual(cm.exception.cls, ErrorClass.TIMEOUT)
        self.assertTrue(cm.exception.usage_uncertain)

    def test_connection_refused_is_certain_no_usage(self):
        cfg = ModelConfig(name="m", base_url="http://127.0.0.1:9/v1")
        with self.assertRaises(ModelError) as cm:
            OpenAIChatClient(cfg, "k").complete(MSGS, [TOOL], timeout_s=2)
        self.assertEqual(cm.exception.cls, ErrorClass.NETWORK)
        self.assertFalse(cm.exception.usage_uncertain)

    def test_anthropic_endpoint_and_headers(self):
        body = {"content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}],
                "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 2}}
        with ScriptedServer([(200, {}, json.dumps(body))]) as srv:
            cfg = ModelConfig(name="m", base_url=srv.url, provider="anthropic_messages")
            turn = AnthropicClient(cfg, "sk-ant").complete(MSGS, [TOOL], timeout_s=5)
        req = srv.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], "sk-ant")
        self.assertEqual(req["body"]["tools"][0]["input_schema"]["required"], ["command"])
        self.assertEqual(turn.tool_calls[0].arguments, {"command": "ls"})

    def test_error_in_200_body(self):
        with ScriptedServer([(200, {}, '{"error": {"message": "context_length_exceeded", "code": 400}}')]) as srv:
            with self.assertRaises(ModelError) as cm:
                self.client(srv.url).complete(MSGS, [TOOL], timeout_s=5)
        self.assertEqual(cm.exception.cls, ErrorClass.CONTEXT_OVERFLOW)


def oa_chunk(delta=None, finish=None, usage=None):
    c = {"object": "chat.completion.chunk", "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


OA_STREAM = [
    oa_chunk({"role": "assistant", "content": "Let me "}),
    oa_chunk({"content": "look."}),
    oa_chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"comm'}}]}),
    oa_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'and": "ls"}'}}]}),
    oa_chunk({"tool_calls": [{"index": 1, "id": "c2", "type": "function", "function": {"name": "bash", "arguments": '{"command": "pwd"}'}}]}),
    oa_chunk(finish="tool_calls"),
    oa_chunk(usage={"prompt_tokens": 50, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 10}}),
    "[DONE]",
]

AN_STREAM = [
    ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12, "cache_read_input_tokens": 100, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Running."}}),
    ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t1", "name": "bash", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"command": '}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"ls -la"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 2}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}}),
    ("message_stop", {"type": "message_stop"}),
]


@mock.patch.dict(os.environ, NO_PROXY_ENV)
class StreamingTest(unittest.TestCase):
    def cfg(self, url, provider="openai_chat"):
        return ModelConfig(name="m", base_url=url + ("/v1" if provider == "openai_chat" else ""), provider=provider,
                           stream=True)

    def test_openai_stream_reassembles_tool_calls_and_usage(self):
        with ScriptedServer([("sse", OA_STREAM, 0)]) as srv:
            turn = OpenAIChatClient(self.cfg(srv.url), "k").complete(MSGS, [TOOL], timeout_s=5)
        body = srv.requests[0]["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(turn.text, "Let me look.")
        self.assertEqual([(c.id, c.arguments) for c in turn.tool_calls], [("c1", {"command": "ls"}), ("c2", {"command": "pwd"})])
        self.assertEqual((turn.usage.input_tokens, turn.usage.cache_read_tokens, turn.usage.output_tokens), (40, 10, 9))
        self.assertEqual(turn.finish_reason, "tool_calls")

    def test_openai_stream_without_done_but_finished_is_complete(self):
        with ScriptedServer([("sse", OA_STREAM[:-1], 0)]) as srv:
            turn = OpenAIChatClient(self.cfg(srv.url), "k").complete(MSGS, [TOOL], timeout_s=5)
        self.assertEqual(len(turn.tool_calls), 2)

    def test_openai_stream_cut_midway_is_uncertain_network_error(self):
        with ScriptedServer([("sse", OA_STREAM[:3] + ["CUT"], 0)]) as srv:
            with self.assertRaises(ModelError) as cm:
                OpenAIChatClient(self.cfg(srv.url), "k").complete(MSGS, [TOOL], timeout_s=5)
        self.assertEqual(cm.exception.cls, ErrorClass.NETWORK)
        self.assertTrue(cm.exception.usage_uncertain)

    def test_stream_total_deadline_enforced(self):
        slow = [oa_chunk({"content": "x"})] * 20 + ["[DONE]"]
        t0 = time.monotonic()
        with ScriptedServer([("sse", slow, 0.2)]) as srv:
            with self.assertRaises(ModelError) as cm:
                OpenAIChatClient(self.cfg(srv.url), "k").complete(MSGS, [TOOL], timeout_s=1.0)
        self.assertEqual(cm.exception.cls, ErrorClass.TIMEOUT)
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_stream_error_event_classified(self):
        ev = [("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})]
        with ScriptedServer([("sse", ev, 0)]) as srv:
            with self.assertRaises(ModelError) as cm:
                AnthropicClient(self.cfg(srv.url, "anthropic_messages"), "k").complete(MSGS, [TOOL], timeout_s=5)
        self.assertEqual(cm.exception.cls, ErrorClass.SERVER)

    def test_anthropic_stream_keeps_thinking_signature_and_tool_input(self):
        with ScriptedServer([("sse", AN_STREAM, 0)]) as srv:
            turn = AnthropicClient(self.cfg(srv.url, "anthropic_messages"), "k").complete(MSGS, [TOOL], timeout_s=5)
        self.assertTrue(srv.requests[0]["body"]["stream"])
        self.assertEqual(turn.text, "Running.")
        self.assertEqual(turn.tool_calls[0].arguments, {"command": "ls -la"})
        self.assertEqual(turn.provider_raw[0], {"type": "thinking", "thinking": "hmm", "signature": "SIG"})
        self.assertEqual((turn.usage.input_tokens, turn.usage.cache_read_tokens, turn.usage.output_tokens), (12, 100, 30))

    def test_server_ignoring_stream_flag_returns_json(self):
        with ScriptedServer([ok_openai()]) as srv:
            turn = OpenAIChatClient(self.cfg(srv.url), "k").complete(MSGS, [TOOL], timeout_s=5)
        self.assertEqual(turn.tool_calls[0].name, "bash")
