"""Regressions for issues found in the first live runs (DeepSeek via NVIDIA NIM, Qwen via OpenRouter)."""

from __future__ import annotations

import random
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from gheerefill.config import ConfigError, load_profile
from gheerefill.models.base import ErrorClass, ModelError, call_with_retry
from gheerefill.resolve import choose_generic, choose_model, resolve
from tests.test_models import MSGS, TOOL, ok_openai  # noqa: F401  (shared fixtures)


class _Flaky:
    """Fails with `cls` `n` times, then answers."""

    def __init__(self, n: int, cls: ErrorClass):
        self.n, self.cls, self.calls = n, cls, 0

    def complete(self, messages, tools, *, timeout_s, total_s=None):
        self.calls += 1
        if self.calls <= self.n:
            raise ModelError(self.cls, f"{self.cls.value} #{self.calls}", status=429)
        from gheerefill.models.base import ModelTurn, Usage
        return ModelTurn(text="ok", tool_calls=[], finish_reason="stop", usage=Usage())


def _call(client, *, window: float, max_attempts: int = 3, time_left=lambda: 5000.0):
    sleeps: list[float] = []
    turn = call_with_retry(client, MSGS, [TOOL], max_attempts=max_attempts, base_delay_s=2.0, max_delay_s=60.0,
                           time_left=time_left, request_timeout_s=5, on_attempt=lambda r: None,
                           rng=random.Random(0), sleep=sleeps.append, min_call_s=0.1, transient_window_s=window)
    return turn, sleeps


class RateLimitWindowTest(unittest.TestCase):
    def test_rate_limit_storm_is_outlasted_within_the_window(self):
        # live: a shared/free key answered 429 for minutes; 6 fixed attempts ended the task after 48 s
        client = _Flaky(12, ErrorClass.RATE_LIMIT)
        turn, sleeps = _call(client, window=600.0)
        self.assertEqual(turn.text, "ok")
        self.assertEqual(client.calls, 13)
        self.assertLess(sum(sleeps), 600.0 + 60.0)

    def test_overloaded_server_is_retried_in_the_window(self):
        client = _Flaky(8, ErrorClass.SERVER)
        turn, _ = _call(client, window=600.0)
        self.assertEqual(turn.text, "ok")

    def test_window_is_bounded(self):
        client = _Flaky(10_000, ErrorClass.RATE_LIMIT)
        with self.assertRaises(ModelError) as cm:
            _call(client, window=300.0)
        self.assertEqual(cm.exception.cls, ErrorClass.RATE_LIMIT)
        self.assertLess(client.calls, 20)

    def test_without_window_old_bound_holds(self):
        client = _Flaky(12, ErrorClass.RATE_LIMIT)
        with self.assertRaises(ModelError):
            _call(client, window=0.0)
        self.assertEqual(client.calls, 3)

    def test_unreachable_endpoint_still_fails_after_max_attempts(self):
        client = _Flaky(12, ErrorClass.NETWORK)
        with self.assertRaises(ModelError):
            _call(client, window=600.0)
        self.assertEqual(client.calls, 3)

    def test_deadline_still_wins(self):
        client = _Flaky(12, ErrorClass.RATE_LIMIT)
        with self.assertRaises(ModelError) as cm:
            _call(client, window=600.0, time_left=lambda: 20.0)
        self.assertEqual(cm.exception.cls, ErrorClass.DEADLINE)

    def test_default_profile_enables_the_window(self):
        self.assertGreaterEqual(load_profile(ROOT / "profiles" / "default.toml", env={}).retry.transient_window_s, 300.0)


class ModelResolutionTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(ROOT / "profiles" / "default.toml", env={})

    def test_openrouter_short_date_suffix_is_a_variant(self):
        # live: OpenRouter lists `qwen/qwen3.8-max-0902`; the key silently fell through to DeepSeek
        avail = ["deepseek/deepseek-v4-pro", "qwen/qwen3.8-max-0902", "qwen/qwen3.8-max-prime"]
        self.assertEqual(choose_model(["qwen/qwen3.8-max", "deepseek/deepseek-v4-pro"], avail), "qwen/qwen3.8-max-0902")
        r = resolve(self.profile, "sk-or-v1-" + "a" * 64, lister=lambda c, k: avail)
        self.assertEqual(r.model.name, "qwen/qwen3.8-max-0902")

    def test_retired_preferences_fall_back_to_a_served_coder_model(self):
        # live: NVIDIA NIM no longer serves the profile's preferences; the harness refused to start
        nim = ["01-ai/yi-large", "deepseek-ai/deepseek-coder-6.7b-instruct", "deepseek-ai/deepseek-v4.1-flash",
               "meta/llama-guard-4-12b", "openai/gpt-oss-20b"]
        r = resolve(self.profile, "nvapi-" + "x" * 60, lister=lambda c, k: nim)
        self.assertEqual(r.model.name, "deepseek-ai/deepseek-v4.1-flash")  # now the rule's first preference
        # when the provider lists none of the preferences, a served Qwen/DeepSeek coder is taken, with a note
        later = ["01-ai/yi-large", "meta/llama-guard-4-12b", "qwen/qwen3.9-coder-plus", "openai/gpt-oss-20b"]
        r = resolve(self.profile, "nvapi-" + "x" * 60, lister=lambda c, k: later)
        self.assertEqual(r.model.name, "qwen/qwen3.9-coder-plus")
        self.assertTrue(any("none of the preferred models" in n for n in r.notes))

    def test_no_suitable_model_still_refuses(self):
        with self.assertRaises(ConfigError):
            resolve(self.profile, "nvapi-" + "x" * 60, lister=lambda c, k: ["01-ai/yi-large", "meta/llama2-70b"])
        self.assertIsNone(choose_generic(["a", "b"], first_as_last_resort=False))

    def test_pinned_model_is_still_never_substituted(self):
        self.profile.model.name = "prescribed-model-x"
        r = resolve(self.profile, "nvapi-" + "x" * 60, lister=lambda c, k: ["deepseek-ai/deepseek-v4.1-flash"])
        self.assertEqual(r.model.name, "prescribed-model-x")


if __name__ == "__main__":
    unittest.main()
