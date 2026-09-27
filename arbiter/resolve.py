"""Resolve the model configuration from the committed profile and the supplied credential.

The official procedure supplies only AI_API_KEY. The model configuration therefore lives in the
profile (`profiles/default.toml`): `[model]` pins a model when one is prescribed, and ordered
`[[auto]]` rules map the credential's *format* to one provider, endpoint and model preference list.

Rules of engagement:
- The key is sent only to the endpoint(s) of the first rule its format matches. It is never tried
  against providers whose documented key format differs ("key spraying" would leak it to third
  parties). One exception, by necessity: DeepSeek and Alibaba Cloud (Qwen: DashScope regions,
  QwenCloud) issue keys in the same `sk-` + 32 hex format, so that rule lists those endpoints as
  candidates and moves to the next only when one answers 401.
- AI_BASE_URL without a model (a self-hosted vLLM/SGLang/Ollama server, any key) picks from the
  server's own model list: a Qwen or DeepSeek coder first, else the only / first model served.
- A pinned model name (profile, or AI_MODEL) is used as-is. It is never replaced; if the provider's
  model list does not contain it, a warning is recorded and the first request decides.
- With no pinned name, the first entry of the rule's preference list that the provider's
  /models endpoint reports is chosen, and that exact id is recorded. If /models is unavailable,
  the first preference is used and marked unverified.
- An authentication failure on /models stops the run before any work, with a clear message.
"""

from __future__ import annotations

import copy
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from arbiter.config import AUTO, ConfigError, ModelConfig, Profile
from arbiter.models.base import ErrorClass, ModelError, classify_http_error
from arbiter.models.http import USER_AGENT, _ssl_context

# Messages that mean the key itself is wrong (as opposed to lacking a permission for /models).
INVALID_KEY = re.compile(r"invalid.{0,20}(api.?key|x-api-key|token|credential)|incorrect api key|api key not valid|"
                         r"no api key|unauthenticated|authentication (failed|error)|invalid authentication|"
                         r"expired|revoked|not a valid key", re.I)
DATE_SUFFIX = re.compile(r"^(?P<base>.+?)(-\d{8}|-\d{4}-\d{2}-\d{2}|-\d{4}|-latest)$")  # -MMDD: OpenRouter, DeepSeek


@dataclass
class Resolution:
    model: ModelConfig
    source: str
    discovery: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.model.provider, "model": self.model.name, "base_url": self.model.base_url,
                "source": self.source, "discovery": self.discovery, "notes": self.notes}


def rule_matches(match: str, key: str) -> bool:
    kind, _, pattern = match.partition(":")
    if kind == "prefix":
        return key.startswith(pattern)
    if kind == "contains":
        return pattern in key
    if kind == "regex":
        return re.search(pattern, key) is not None
    return False


def list_models(cfg: ModelConfig, key: str, timeout_s: float = 20.0) -> list[str]:
    """GET the provider's model list (no tokens consumed). Raises ModelError."""
    base = cfg.base_url.rstrip("/")
    if cfg.provider == "anthropic_messages":
        url = base + ("/models" if base.endswith("/v1") else "/v1/models") + "?limit=1000"
        headers = {"x-api-key": key, "anthropic-version": cfg.anthropic_version}
    else:
        url = base + "/models"
        headers = {"Authorization": f"Bearer {key}"}
    req = urllib.request.Request(url, headers={**headers, **cfg.extra_headers, "Accept": "application/json",
                                                       "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s,
                                    context=_ssl_context() if url.startswith("https://") else None) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise classify_http_error(e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers or {})) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ModelError(ErrorClass.NETWORK, f"cannot list models: {e}") from None
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = data.get("models", []) if isinstance(data, dict) else []
    ids = []
    for it in items:
        mid = it.get("id") or it.get("name") if isinstance(it, dict) else it
        if isinstance(mid, str):
            ids.append(mid[len("models/"):] if mid.startswith("models/") else mid)
    return ids


def choose_model(preferences: list[str], available: list[str]) -> str | None:
    """First preference present: exact id, else a dated/`-latest` variant of it (newest first)."""
    avail = set(available)
    for pref in preferences:
        if pref in avail:
            return pref
        variants = sorted((a for a in available if (m := DATE_SUFFIX.match(a)) and m.group("base") == pref), reverse=True)
        if variants:
            return variants[0]
    return None


# Self-hosted / unknown OpenAI-compatible servers: which served model to prefer (regex, in order).
GENERIC_PREFS = (r"qwen.*coder", r"deepseek.*(v4|pro)", r"deepseek", r"qwen3\.[5-9]|qwen3-max|qwen-max", r"qwen",
                 r"coder")
RULE_PARAMS = ("max_tokens_field", "context_window", "max_output_tokens", "prompt_cache", "extra_headers",
               "extra_body", "reasoning_passback", "request_timeout_s", "stream")


def choose_generic(available: list[str], first_as_last_resort: bool = True) -> str | None:
    for pat in GENERIC_PREFS:
        hits = [a for a in available if re.search(pat, a, re.I) and not re.search(
            r"embed|rerank|vl|audio|omni|tts|asr|guard|reward|safety", a, re.I)]
        if hits:
            return hits[0]
    return available[0] if available and first_as_last_resort else None


def probe_completion(cfg: ModelConfig, key: str, model: str, timeout_s: float = 30.0) -> str:
    """For endpoints without a model list: does a 1-token request authenticate? ok | auth | other."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}).encode()
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT,
                                          **cfg.extra_headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s,
                                    context=_ssl_context() if cfg.base_url.startswith("https://") else None):
            return "ok"
    except urllib.error.HTTPError as e:
        return "auth" if e.code == 401 else "other"
    except (urllib.error.URLError, OSError, ValueError):
        return "other"


def _candidates(rule: dict[str, Any]) -> list[dict[str, Any]]:
    base = {k: v for k, v in rule.items() if k != "candidates"}
    return [base] + [{**base, **c} for c in rule.get("candidates") or []]


def _apply_rule(m: ModelConfig, cand: dict[str, Any], base_override: str) -> ModelConfig:
    c = copy.deepcopy(m)
    c.provider = cand["provider"]
    c.base_url = base_override or cand["base_url"]
    for k in RULE_PARAMS:
        if k in cand:
            v = cand[k]
            if k in ("extra_headers", "extra_body"):
                v = {**v, **getattr(m, k)}  # explicit profile values win
            setattr(c, k, copy.deepcopy(v))
    return c


def resolve(profile: Profile, key: str, *, discover: bool = True,
            lister: Callable[[ModelConfig, str], list[str]] = list_models,
            prober: Callable[[ModelConfig, str, str], str] = probe_completion) -> Resolution:
    m = copy.deepcopy(profile.model)
    notes: list[str] = []
    if m.provider == "fake":
        return Resolution(m, "profile (scripted fake model)", "not applicable")
    rule = None
    generic = False
    candidates: list[dict[str, Any]] = []
    if m.provider == AUTO:
        rule = next((r for r in profile.auto if rule_matches(str(r["match"]), key)), None)
        if m.base_url and (m.name or rule is None):
            m.provider = "openai_chat"
            generic = not m.name
            source = "AI_BASE_URL/AI_MODEL override (OpenAI-compatible)" if m.name else \
                "AI_BASE_URL override (OpenAI-compatible); model chosen from the server's list"
            rule = None
        elif rule is None:
            raise ConfigError(
                "cannot infer the provider from the AI_API_KEY format (no [[auto]] rule matches). "
                "Set AI_BASE_URL (and AI_MODEL) or pin [model] in profiles/default.toml. The key is never "
                "tried against other providers."
            )
        else:
            candidates = [_apply_rule(m, c, m.base_url) for c in _candidates(rule)]
            if m.base_url:  # AI_BASE_URL with a recognised key: that endpoint only
                candidates = candidates[:1]
                generic = True
            m = candidates[0]
            source = f"auto rule '{rule.get('label', rule['match'])}' (key format {rule['match']})"
    else:
        source = f"profile {profile.name!r}"
    if profile.overrides:
        source += f"; overrides: {', '.join(f'{k}<-{v}' for k, v in profile.overrides.items())}"
    discovery = "skipped"
    available: list[str] | None = None
    prefs: list[str] = []
    if discover:
        tried: list[str] = []
        for idx, cand_m in enumerate(candidates or [m]):
            cand = _candidates(rule)[idx] if rule else {}
            label = cand.get("label", cand_m.base_url)
            last = idx == len(candidates or [m]) - 1
            try:
                available = lister(cand_m, key)
                m, discovery = cand_m, f"{len(available)} models listed by the provider"
                if rule:
                    prefs = list(cand.get("models") or rule["models"])
                if tried:
                    notes.append(f"key not accepted by {', '.join(tried)}; accepted by {label}")
                break
            except ModelError as e:
                key_unknown_here = e.cls == ErrorClass.AUTH and (e.status == 401 or INVALID_KEY.search(e.message))
                if key_unknown_here and not last:  # another vendor issuing this key format may know it
                    tried.append(label)
                    continue
                if e.cls == ErrorClass.QUOTA or (e.cls == ErrorClass.AUTH and (INVALID_KEY.search(e.message) or tried)):
                    where = f" (tried {', '.join(tried + [label])})" if tried else ""
                    raise ConfigError(f"the provider rejected AI_API_KEY ({e.cls.value}){where}: {e.message[:200]}") from None
                if len(candidates) > 1 and not last and rule:
                    # no usable model list: a 1-token request tells whether this endpoint knows the key
                    verdict = prober(cand_m, key, (cand.get("models") or rule["models"])[0])
                    if verdict == "auth":
                        tried.append(label)
                        continue
                # Restricted keys may be allowed to call the model but not to list models (401/403 with a
                # permission/scope message): continue, and let the first real request decide.
                m, available = cand_m, None
                if rule:
                    prefs = list(cand.get("models") or rule["models"])
                discovery = f"model list unavailable ({e.cls.value}: {e.message[:80]}); choice unverified until the first request"
                break
    elif rule:
        prefs = list(rule["models"])
    if m.name:
        if available is not None and m.name not in available and choose_model([m.name], available) is None:
            notes.append(f"pinned model {m.name!r} is not in the provider's model list; using it anyway "
                         "(never substituted); the first request will confirm")
    else:
        chosen = choose_model(prefs, available) if (available and prefs) else None
        if chosen is None and available and generic:
            chosen = choose_generic(available)
            if chosen:
                notes.append(f"model {chosen!r} chosen from the server's list {sorted(available)[:6]}")
        if chosen is None and available and prefs and rule:
            # the provider renamed or retired every listed preference: take a Qwen/DeepSeek/coder model it
            # does serve rather than refuse to start (no model is pinned, so nothing is substituted)
            chosen = choose_generic(available, first_as_last_resort=False)
            if chosen:
                notes.append(f"none of the preferred models {prefs} is listed by the provider; using {chosen!r} "
                             "from its model list")
        if chosen is None and available and prefs:
            raise ConfigError(
                f"none of the configured models {prefs} is available with this key "
                f"(provider lists e.g. {sorted(available)[:8]}). Pin one with AI_MODEL or [model].name."
            )
        if chosen is None and not prefs:
            raise ConfigError("no model configured: set [model].name or AI_MODEL (the server did not list its models)")
        m.name = chosen or prefs[0]
        if chosen is None:
            notes.append(f"model list unavailable: using first preference {m.name!r} (unverified)")
    return Resolution(m, source, discovery, notes)
