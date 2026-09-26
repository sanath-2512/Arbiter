"""Resolve the model configuration from the committed profile and the supplied credential.

The official procedure supplies only AI_API_KEY. The model configuration therefore lives in the
profile (`profiles/default.toml`): `[model]` pins a model when one is prescribed, and ordered
`[[auto]]` rules map the credential's *format* to one provider, endpoint and model preference list.

Rules of engagement:
- The key is sent only to the endpoint of the first rule its format matches. It is never tried
  against other providers ("key spraying" would leak it to third parties).
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

from gheerefill.config import AUTO, ConfigError, ModelConfig, Profile
from gheerefill.models.base import ErrorClass, ModelError, classify_http_error
from gheerefill.models.http import _ssl_context  # noqa: F401 (TLS policy shared with the model client)

DATE_SUFFIX = re.compile(r"^(?P<base>.+?)(-\d{8}|-\d{4}-\d{2}-\d{2}|-latest)$")


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
    req = urllib.request.Request(url, headers={**headers, **cfg.extra_headers, "Accept": "application/json"})
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


def resolve(profile: Profile, key: str, *, discover: bool = True,
            lister: Callable[[ModelConfig, str], list[str]] = list_models) -> Resolution:
    m = copy.deepcopy(profile.model)
    notes: list[str] = []
    if m.provider == "fake":
        return Resolution(m, "profile (scripted fake model)", "not applicable")
    rule = None
    if m.provider == AUTO:
        if m.base_url and m.name:
            m.provider = "openai_chat"
            source = "AI_BASE_URL/AI_MODEL override (OpenAI-compatible)"
        else:
            rule = next((r for r in profile.auto if rule_matches(str(r["match"]), key)), None)
            if rule is None:
                raise ConfigError(
                    "cannot infer the provider from the AI_API_KEY format (no [[auto]] rule matches). "
                    "Set AI_BASE_URL (and AI_MODEL) or pin [model] in profiles/default.toml. The key is never "
                    "tried against other providers."
                )
            m.provider = rule["provider"]
            m.base_url = m.base_url or rule["base_url"]
            for k in ("max_tokens_field", "context_window", "max_output_tokens"):
                if k in rule:
                    setattr(m, k, rule[k])
            source = f"auto rule '{rule.get('label', rule['match'])}' (key format {rule['match']})"
    else:
        source = f"profile {profile.name!r}"
    if profile.overrides:
        source += f"; overrides: {', '.join(f'{k}<-{v}' for k, v in profile.overrides.items())}"
    discovery = "skipped"
    if discover:
        try:
            available = lister(m, key)
            discovery = f"{len(available)} models listed by the provider"
        except ModelError as e:
            if e.cls in (ErrorClass.AUTH, ErrorClass.QUOTA):
                raise ConfigError(f"the provider rejected AI_API_KEY ({e.cls.value}): {e.message[:200]}") from None
            available = None
            discovery = f"model list unavailable ({e.cls.value}); choice unverified until the first request"
    else:
        available = None
    if m.name:
        if available is not None and m.name not in available and choose_model([m.name], available) is None:
            notes.append(f"pinned model {m.name!r} is not in the provider's model list; using it anyway "
                         "(never substituted); the first request will confirm")
    else:
        prefs = list(rule["models"]) if rule else []
        if not prefs:
            raise ConfigError("no model configured: set [model].name or AI_MODEL")
        chosen = choose_model(prefs, available) if available else None
        if chosen is None and available:
            raise ConfigError(
                f"none of the configured models {prefs} is available with this key "
                f"(provider lists e.g. {sorted(available)[:8]}). Pin one with AI_MODEL or [model].name."
            )
        m.name = chosen or prefs[0]
        if chosen is None:
            notes.append(f"model list unavailable: using first preference {m.name!r} (unverified)")
    return Resolution(m, source, discovery, notes)
