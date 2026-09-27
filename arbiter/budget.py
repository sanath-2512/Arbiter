"""Monotonic deadline and a single ledger for every submitted-runtime cost.

Covers model requests (every attempt, including failed and retried ones), tool time,
setup and finalisation. Usage from requests whose billing is uncertain (interrupted,
timed out, 5xx) is counted in `requests_usage_unknown`, never silently as zero.
Cost is only computed when the profile supplies pricing; otherwise it is `null`.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Callable

from arbiter.config import LimitsConfig
from arbiter.models.base import AttemptRecord


class Budget:
    def __init__(self, limits: LimitsConfig, pricing: dict[str, float] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.pricing = pricing or {}
        self.clock = clock
        self.start = clock()
        self.deadline = self.start + limits.time_limit_s
        self.attempts: list[dict[str, Any]] = []
        self.tok = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "reasoning_tokens": 0}
        self.requests = 0
        self.requests_ok = 0
        self.requests_usage_unknown = 0
        self.model_time_s = 0.0
        self.tool_time_s = 0.0
        self.tool_counts: dict[str, int] = {}
        self.phase_s: dict[str, float] = {}
        self.steps = 0
        self.max_call_latency_s = 0.0

    # time
    def elapsed(self) -> float:
        return self.clock() - self.start

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def work_remaining(self) -> float:
        """Time available for exploration: excludes the finalisation reserve."""
        return self.remaining() - self.limits.finalize_reserve_s

    # accounting
    def record_attempt(self, rec: AttemptRecord, role: str = "solver") -> None:
        self.requests += 1
        self.model_time_s += rec.latency_s
        self.max_call_latency_s = max(self.max_call_latency_s, rec.latency_s)
        d = asdict(rec)
        d["role"] = role
        self.attempts.append(d)
        if rec.outcome == "ok" and rec.usage and rec.usage.get("known", True):
            self.requests_ok += 1
            for k in self.tok:
                self.tok[k] += int(rec.usage.get(k, 0) or 0)
        elif rec.outcome == "ok":
            self.requests_ok += 1
            self.requests_usage_unknown += 1
        elif rec.usage_uncertain:
            self.requests_usage_unknown += 1

    def record_tool(self, name: str, duration_s: float) -> None:
        self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
        self.tool_time_s += duration_s

    def add_phase(self, phase: str, seconds: float) -> None:
        self.phase_s[phase] = self.phase_s.get(phase, 0.0) + seconds

    def total_tokens(self) -> int:
        return (self.tok["input_tokens"] + self.tok["output_tokens"] + self.tok["cache_read_tokens"]
                + self.tok["cache_write_tokens"])

    def cost_usd(self) -> float | None:
        if not self.pricing:
            return None
        p = self.pricing
        return round(
            (self.tok["input_tokens"] * p.get("input", 0.0)
             + self.tok["output_tokens"] * p.get("output", 0.0)
             + self.tok["cache_read_tokens"] * p.get("cache_read", p.get("input", 0.0))
             + self.tok["cache_write_tokens"] * p.get("cache_write", p.get("input", 0.0))) / 1e6,
            6,
        )

    def stop_reason(self) -> str | None:
        if self.steps >= self.limits.max_steps:
            return "step_limit"
        if self.work_remaining() <= 0:
            return "deadline_reached"
        if self.limits.max_total_tokens and self.total_tokens() >= self.limits.max_total_tokens:
            return "token_budget"
        cost = self.cost_usd()
        if self.limits.max_cost_usd and cost is not None and cost >= self.limits.max_cost_usd:
            return "cost_budget"
        return None

    def summary(self) -> dict[str, Any]:
        cost = self.cost_usd()
        return {
            **self.tok,
            "total_tokens": self.total_tokens(),
            "requests": self.requests,
            "requests_ok": self.requests_ok,
            "requests_usage_unknown": self.requests_usage_unknown,
            "usage_complete": self.requests_usage_unknown == 0,
            "cost_usd": cost,
            "cost_note": None if cost is not None else "pricing not configured in profile; cost not computed",
            "model_time_s": round(self.model_time_s, 3),
            "tool_time_s": round(self.tool_time_s, 3),
            "tool_calls": dict(self.tool_counts),
            "steps": self.steps,
        }
