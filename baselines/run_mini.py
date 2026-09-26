#!/usr/bin/env python3
"""Run the pinned upstream mini-swe-agent (2.4.6) on one task, matched to a gheerefill profile.

Development-only baseline; executed with .venv-baseline/bin/python (see setup_baselines.sh).

Upstream components used unchanged: DefaultAgent, LocalEnvironment, LitellmModel and the builtin
`mini.yaml` config (system/instance templates, observation and format-error templates, env vars).

Documented adapter changes (for matched conditions, not capability):
- step_limit / wall_time_limit_seconds come from the same profile/task limits as our harness;
  cost_limit is disabled (0) because our harness has no cost cap by default.
- The same model id/endpoint/max_tokens/temperature/extra_body as the profile, via litellm's
  `openai/` or `anthropic/` provider prefix with `api_base`.
- cost_tracking="ignore_errors": custom endpoints have no litellm price entry; cost is reported
  as unavailable rather than failing the run.
- The API key is passed only to the model and removed from os.environ before any command runs
  (upstream LocalEnvironment forwards os.environ to model-issued commands).
- The global mini-swe-agent config dir is isolated per run so no user .env leaks in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_ACCESS_KEY", "_PASSWORD")


def load_profile(path: Path) -> dict:
    data = tomllib.loads(path.read_text())
    model = data.get("model", {})
    for var, key in (("AI_MODEL", "name"), ("AI_BASE_URL", "base_url"), ("AI_PROVIDER", "provider")):
        if os.environ.get(var):
            model[key] = os.environ[var]
    data["model"] = model
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--issue-file", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--time-limit", type=float, required=True)
    ap.add_argument("--max-steps", type=int, required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(out / "mswea-config")
    os.environ["MSWEA_COST_TRACKING"] = "ignore_errors"

    prof = load_profile(Path(args.profile))
    m = prof["model"]
    provider = m.get("provider", "openai_chat")
    if provider not in ("openai_chat", "anthropic_messages"):
        print(f"baseline supports live providers only, got {provider!r}", file=sys.stderr)
        return 2
    if not m.get("name") or not m.get("base_url"):
        print("model name/base_url not configured (profile or AI_MODEL/AI_BASE_URL)", file=sys.stderr)
        return 2
    key_env = m.get("api_key_env", "AI_API_KEY")
    api_key = os.environ.pop(key_env, "")
    if not api_key:
        print(f"{key_env} is not set", file=sys.stderr)
        return 2
    for k in list(os.environ):
        if k.upper().endswith(SECRET_SUFFIXES):
            os.environ.pop(k)

    from minisweagent.agents.default import DefaultAgent
    from minisweagent.config import get_config_from_spec
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models.litellm_model import LitellmModel
    import minisweagent

    cfg = get_config_from_spec("mini.yaml")
    agent_cfg = {k: v for k, v in cfg.get("agent", {}).items() if k != "mode"}
    agent_cfg.update(step_limit=args.max_steps, cost_limit=0.0, wall_time_limit_seconds=int(args.time_limit),
                     output_path=out / "trajectory.json")
    prefix = "openai" if provider == "openai_chat" else "anthropic"
    base_url = m["base_url"].rstrip("/")
    if prefix == "anthropic":
        base_url = re.sub(r"/v1$", "", base_url)
    model_kwargs = {"api_base": base_url, "api_key": api_key, "max_tokens": int(m.get("max_output_tokens", 8192))}
    if m.get("temperature") is not None:
        model_kwargs["temperature"] = m["temperature"]
    model_kwargs.update(m.get("extra_body", {}) or {})
    model_cfg = {k: v for k, v in cfg.get("model", {}).items() if k in ("observation_template", "format_error_template")}
    model = LitellmModel(model_name=f"{prefix}/{m['name']}", model_kwargs=model_kwargs, cost_tracking="ignore_errors",
                         **model_cfg)
    env_cfg = cfg.get("environment", {})
    tools = prof.get("tools", {})
    env = LocalEnvironment(cwd=str(Path(args.repo).resolve()), env=env_cfg.get("env", {}) or {},
                           timeout=int(tools.get("bash_timeout_s", 180)))
    agent = DefaultAgent(model, env, **agent_cfg)
    issue = Path(args.issue_file).read_text()
    t0 = time.monotonic()
    status, error = "", None
    try:
        info = agent.run(task=issue)
        status = info.get("exit_status", "")
    except Exception as e:  # noqa: BLE001 - record and exit cleanly
        status, error = type(e).__name__, str(e).replace(api_key, "[REDACTED]")[:2000]
    elapsed = time.monotonic() - t0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "responses_without_usage": 0}
    for msg in agent.messages:
        resp = (msg.get("extra") or {}).get("response")
        if msg.get("role") != "assistant" or not isinstance(resp, dict):
            continue
        u = resp.get("usage") or {}
        if not u:
            usage["responses_without_usage"] += 1
            continue
        usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage["cached_tokens"] += int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    record = {
        "system": "mini-swe-agent",
        "version": minisweagent.__version__,
        "exit_status": status,
        "error": error,
        "api_calls": agent.n_calls,
        "steps": agent.n_calls,
        "elapsed_s": round(elapsed, 3),
        "usage": usage,
        "cost_usd": None,
        "live": True,
        "model": {"provider": provider, "name": m["name"], "base_url": m["base_url"]},
    }
    (out / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
