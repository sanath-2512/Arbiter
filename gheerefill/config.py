"""Validated, reproducible run profiles (TOML).

A profile pins the model identity, endpoint, generation settings, tool set, limits and
policies.  Three environment variables may override profile values; the effective value
and its source are recorded in every result.  The credential itself is never stored in a
profile: it is read from the environment variable named by `model.api_key_env`
(default `AI_API_KEY`) inside the model-client boundary only.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10: the vendored upstream of tomllib
    from gheerefill._vendor import tomli as tomllib  # type: ignore[no-redef]

PROVIDERS = ("openai_chat", "anthropic_messages", "fake")
AUTO = "auto"  # provider resolved from the credential's format via the profile's [[auto]] rules
TOOL_PROTOCOLS = ("native", "text")
TOOL_SETS = ("full", "bash_only")

ENV_OVERRIDES = {
    "AI_MODEL": ("model", "name"),
    "AI_BASE_URL": ("model", "base_url"),
    "AI_PROVIDER": ("model", "provider"),
}


class ConfigError(ValueError):
    pass


@dataclass
class ModelConfig:
    provider: str = "openai_chat"
    name: str = ""
    base_url: str = ""
    api_key_env: str = "AI_API_KEY"
    tool_protocol: str = "native"
    max_output_tokens: int = 8192
    # OpenAI-compatible endpoints differ: "max_tokens" or "max_completion_tokens" or "" (omit).
    max_tokens_field: str = "max_tokens"
    context_window: int = 128000
    request_timeout_s: float = 300.0
    temperature: float | None = None
    system_role: str = "system"
    anthropic_version: str = "2023-06-01"
    prompt_cache: bool = False
    # Server-sent-event streaming (avoids gateway idle timeouts on slow responses; enforces the
    # deadline mid-response). stream_usage requests usage in the final chunk (OpenAI-compatible).
    stream: bool = False
    stream_usage: bool = True
    # Passed verbatim into the request body (e.g. reasoning_effort, top_p, thinking).
    extra_body: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    # USD per million tokens. Empty => cost reported as unavailable, never as zero.
    pricing: dict[str, float] = field(default_factory=dict)
    # Fake provider only: path to a scripted-turn JSON file (deterministic tests/replay).
    script: str = ""


@dataclass
class RetryConfig:
    max_attempts: int = 6
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    seed: int = 0


@dataclass
class LimitsConfig:
    time_limit_s: float = 1800.0
    max_steps: int = 150
    max_total_tokens: int = 0  # 0 = no harness-side token cap
    max_cost_usd: float = 0.0  # 0 = no harness-side cost cap (requires pricing to enforce)
    finalize_reserve_s: float = 45.0
    max_consecutive_format_errors: int = 4


@dataclass
class ToolsConfig:
    set: str = "full"
    bash_timeout_s: float = 180.0
    max_observation_chars: int = 12000
    read_max_lines: int = 400
    max_output_bytes: int = 64 * 1024 * 1024
    search_max_results: int = 200
    # Refuse edits that would turn a parseable .py/.json file into an unparseable one (SWE-agent's
    # edit-time linting finding); confirmed with the project's own python3 before refusing.
    syntax_guard: bool = True


@dataclass
class PolicyConfig:
    repo_overview: bool = True
    submit_review: bool = True
    recover_empty_final: bool = True
    dominance_selection: bool = True
    final_recheck: bool = True
    budget_notices: bool = True
    repetition_notice: bool = True
    git_hygiene: bool = True
    # Model commands and target-repo git calls run in a Landlock domain: "key" isolates the
    # credential (no file-system restriction), "confine" also limits writes, "off" disables.
    sandbox: str = "key"
    context_reduce_at: float = 0.65
    keep_recent_messages: int = 10
    # Proof-carrying patches (proof.py): at submit the harness compares the agent's checks and
    # registered reproductions on the original code, the counterfactual and the candidate.
    verify_at_submit: bool = True
    # Deterministic localisation hints in the first prompt (locate.py: issue anchors + BM25).
    localize: bool = True
    # Carry execution-verified facts (working test/install commands) to later runs on the same
    # repository (memory.py). Never code, patches or issue text.
    memory: bool = True
    # Adaptive attempts: another attempt (from the original code, with the harness's observations)
    # when a submitted candidate's evidence is below `retry_below` (default: only when the evidence
    # refutes it), or when an attempt used its time share without submitting a verified change;
    # always only if enough time remains.
    max_attempts: int = 3
    first_attempt_share: float = 0.6
    retry_below: str = "unverified"
    min_attempt_s: float = 120.0


@dataclass
class Profile:
    name: str = "default"
    model: ModelConfig = field(default_factory=ModelConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    # Ordered [[auto]] rules: credential format -> provider, endpoint, model preference list.
    auto: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def identity(self) -> str:
        """Content hash of settings that affect behaviour (excludes provenance fields)."""
        d = self.to_dict()
        d.pop("source", None)
        d.pop("overrides", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


_SECTIONS = {
    "model": ModelConfig,
    "retry": RetryConfig,
    "limits": LimitsConfig,
    "tools": ToolsConfig,
    "policy": PolicyConfig,
}


def _coerce(section: str, key: str, value: Any, template: Any, annotation: str) -> Any:
    where = f"[{section}].{key}"
    if "dict" in annotation:
        if not isinstance(value, dict):
            raise ConfigError(f"{where} must be a table, got {type(value).__name__}")
        return dict(value)
    if annotation.startswith("float"):
        if value is None and "None" in annotation:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number, got {value!r}")
        return float(value)
    if annotation == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer, got {value!r}")
        return value
    if annotation == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true/false, got {value!r}")
        return value
    if annotation == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string, got {value!r}")
        return value
    return value


AUTO_RULE_KEYS = {"label", "match", "provider", "base_url", "models", "max_tokens_field", "context_window",
                  "max_output_tokens", "prompt_cache"}


def _apply(profile: Profile, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "auto":
            if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
                raise ConfigError("[[auto]] must be an array of tables")
            for i, r in enumerate(value):
                unknown = set(r) - AUTO_RULE_KEYS
                if unknown:
                    raise ConfigError(f"[[auto]] rule {i}: unknown key(s) {sorted(unknown)}")
                if not all(k in r for k in ("match", "provider", "base_url", "models")):
                    raise ConfigError(f"[[auto]] rule {i}: needs match, provider, base_url, models")
                if r["provider"] not in ("openai_chat", "anthropic_messages"):
                    raise ConfigError(f"[[auto]] rule {i}: provider must be openai_chat or anthropic_messages")
                if not isinstance(r["models"], list) or not r["models"]:
                    raise ConfigError(f"[[auto]] rule {i}: models must be a non-empty list")
                kind = str(r["match"]).split(":", 1)[0]
                if kind not in ("prefix", "contains", "regex"):
                    raise ConfigError(f"[[auto]] rule {i}: match must start with prefix:, contains: or regex:")
            profile.auto = [dict(r) for r in value]
            continue
        if key == "name":
            if not isinstance(value, str):
                raise ConfigError("profile name must be a string")
            profile.name = value
            continue
        if key not in _SECTIONS:
            raise ConfigError(f"unknown profile section or key: {key!r}")
        if not isinstance(value, dict):
            raise ConfigError(f"[{key}] must be a table")
        section_obj = getattr(profile, key)
        fields = {f.name: f for f in dataclasses.fields(section_obj)}
        for k, v in value.items():
            if k not in fields:
                raise ConfigError(f"unknown key [{key}].{k} (allowed: {', '.join(sorted(fields))})")
            ann = fields[k].type if isinstance(fields[k].type, str) else str(fields[k].type)
            setattr(section_obj, k, _coerce(key, k, v, getattr(section_obj, k), ann))


def validate(profile: Profile, *, require_model: bool = True) -> None:
    m = profile.model
    if m.provider == AUTO:
        if not profile.auto and not (m.base_url and m.name):
            raise ConfigError("model.provider = 'auto' needs [[auto]] rules (or AI_BASE_URL and AI_MODEL)")
        return _validate_rest(profile)
    if m.provider not in PROVIDERS:
        raise ConfigError(f"model.provider must be one of {PROVIDERS + (AUTO,)}, got {m.provider!r}")
    if m.provider != "fake" and require_model:
        if not m.name.strip():
            raise ConfigError(
                "model.name is not configured. Set it in the profile or via AI_MODEL. "
                "The harness never substitutes a default model."
            )
        if not m.base_url.strip():
            raise ConfigError("model.base_url is not configured. Set it in the profile or via AI_BASE_URL.")
        if not m.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"model.base_url must be an http(s) URL, got {m.base_url!r}")
    if m.provider == "fake" and not m.script:
        raise ConfigError("provider 'fake' requires model.script (a scripted-turn JSON file)")
    _validate_rest(profile)


def _validate_rest(profile: Profile) -> None:
    m = profile.model
    if m.tool_protocol not in TOOL_PROTOCOLS:
        raise ConfigError(f"model.tool_protocol must be one of {TOOL_PROTOCOLS}")
    if profile.tools.set not in TOOL_SETS:
        raise ConfigError(f"tools.set must be one of {TOOL_SETS}")
    if m.max_tokens_field not in ("max_tokens", "max_completion_tokens", ""):
        raise ConfigError("model.max_tokens_field must be 'max_tokens', 'max_completion_tokens' or ''")
    for k in m.pricing:
        if k not in ("input", "output", "cache_read", "cache_write"):
            raise ConfigError(f"unknown pricing key {k!r}")
    lim = profile.limits
    if lim.time_limit_s <= 0 or lim.max_steps <= 0:
        raise ConfigError("limits.time_limit_s and limits.max_steps must be positive")
    if lim.finalize_reserve_s < 0 or lim.finalize_reserve_s >= lim.time_limit_s:
        raise ConfigError("limits.finalize_reserve_s must be >= 0 and < time_limit_s")
    if profile.policy.sandbox not in ("off", "key", "confine"):
        raise ConfigError("policy.sandbox must be 'off', 'key' or 'confine'")
    pol = profile.policy
    if pol.max_attempts < 1 or not 0.1 <= pol.first_attempt_share <= 1.0 or pol.min_attempt_s < 0:
        raise ConfigError("policy.max_attempts must be >= 1, first_attempt_share within [0.1, 1], min_attempt_s >= 0")
    if pol.retry_below not in ("refuted", "unverified", "passing", "fixed", "proven"):
        raise ConfigError("policy.retry_below must be one of refuted, unverified, passing, fixed, proven")
    if not 0.1 <= profile.policy.context_reduce_at <= 0.95:
        raise ConfigError("policy.context_reduce_at must be within [0.1, 0.95]")
    if m.max_output_tokens <= 0 or m.context_window <= m.max_output_tokens:
        raise ConfigError("model.context_window must exceed model.max_output_tokens (> 0)")


def load_profile(path: str | Path | None, *, env: dict[str, str] | None = None) -> Profile:
    """Load a profile file, apply documented env overrides. Does not validate credentials."""
    env = os.environ if env is None else env
    profile = Profile()
    if path:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"profile file not found: {p}")
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"invalid TOML in {p}: {e}") from e
        _apply(profile, data)
        profile.source = str(p)
        if profile.model.script and not Path(profile.model.script).is_absolute():
            profile.model.script = str((p.parent / profile.model.script).resolve())
    for var, (section, key) in ENV_OVERRIDES.items():
        val = env.get(var)
        if val:
            setattr(getattr(profile, section), key, val.strip())
            profile.overrides[f"{section}.{key}"] = var
    return profile


def apply_task_limits(profile: Profile, limits: dict[str, Any]) -> Profile:
    """Task-supplied limits take precedence over profile defaults (they come from the evaluator)."""
    p = copy.deepcopy(profile)
    _apply(p, {"limits": limits})
    if "time_limit_s" in limits and "finalize_reserve_s" not in limits:
        # Keep the reserve proportionate for short limits (45 s of a 60 s task would leave 15 s of work).
        p.limits.finalize_reserve_s = min(p.limits.finalize_reserve_s, max(5.0, p.limits.time_limit_s * 0.15))
    if p.limits.finalize_reserve_s >= p.limits.time_limit_s:
        p.limits.finalize_reserve_s = max(5.0, p.limits.time_limit_s * 0.1)
    return p


def profile_from_dict(d: dict[str, Any]) -> Profile:
    """Rebuild a profile from its recorded `to_dict()` form (recovery)."""
    p = Profile()
    data = {k: v for k, v in d.items() if k in _SECTIONS or k in ("name", "auto")}
    _apply(p, data)
    p.source = d.get("source", "")
    p.overrides = dict(d.get("overrides") or {})
    return p
