#!/usr/bin/env python3
"""Run the pinned upstream Pi coding agent (@mariozechner/pi-coding-agent 0.73.1) on one task,
matched to a gheerefill profile. Development-only baseline (install: `bash baselines/setup_baselines.sh`).

Upstream behaviour used unchanged: Pi's default system prompt, default tools (read, bash, edit,
write), its own retries and context handling, non-interactive print mode.

Documented adapter changes (for matched conditions, not capability):
- Model: a custom provider in an isolated PI_CODING_AGENT_DIR/models.json pointing at the same
  endpoint/model id/max tokens/context window as the profile. `supportsDeveloperRole=false` so the
  system prompt goes out as a `system` message, as for our harness.
- Limits: Pi has no step limit; this wrapper counts `turn_end` events and stops the process group
  after `--max-steps` turns or `--time-limit` seconds, whichever comes first.
- Isolation from the developer machine: --no-session, --no-extensions, --no-skills,
  --no-prompt-templates, --no-context-files, --no-themes, --offline.
- Credential: read by Pi through a `!cat <file>` command (file mode 0600) instead of an env var, so
  the key is not inherited by Pi's bash tool; other *_API_KEY/*_TOKEN variables are removed.
- Prompt: the issue text wrapped in one sentence asking for an autonomous fix in the current directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PI_BIN = ROOT / "baselines" / "pi" / "node_modules" / ".bin" / "pi"
sys.path.insert(0, str(ROOT))  # same resolver as the harness: identical model for every system
from gheerefill.config import tomllib  # noqa: E402 - stdlib tomllib, or vendored tomli on 3.9/3.10
SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_ACCESS_KEY", "_PASSWORD")
PROMPT = ("Resolve the following issue by changing the repository in the current directory. "
          "Work autonomously; nobody will answer questions.\n\n<issue>\n{issue}\n</issue>")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--issue-file", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--time-limit", type=float, required=True)
    ap.add_argument("--max-steps", type=int, required=True)
    ap.add_argument("--pi-bin", default=str(PI_BIN))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prof = tomllib.loads(Path(args.profile).read_text())
    key = os.environ.get(prof.get("model", {}).get("api_key_env", "AI_API_KEY"), "")
    if not key:
        print("API key not set", file=sys.stderr)
        return 2
    from gheerefill.config import load_profile
    from gheerefill.resolve import resolve

    m = resolve(load_profile(Path(args.profile)), key,
                discover=os.environ.get("GHEEREFILL_BASELINE_DISCOVER", "1") == "1").model.__dict__
    provider = m["provider"]
    if provider not in ("openai_chat", "anthropic_messages"):
        print("Pi baseline needs a live provider", file=sys.stderr)
        return 2
    if not Path(args.pi_bin).exists():
        print(f"pi not installed at {args.pi_bin}", file=sys.stderr)
        return 2

    agent_dir = out / "pi-agent"
    agent_dir.mkdir(exist_ok=True)
    keyfile = agent_dir / "key"
    keyfile.write_text(key)
    os.chmod(keyfile, 0o600)
    base_url = m["base_url"].rstrip("/")
    if provider == "anthropic_messages":
        base_url = re.sub(r"/v1$", "", base_url)
    model_entry = {"id": m["name"], "contextWindow": int(m.get("context_window", 128000)),
                   "maxTokens": int(m.get("max_output_tokens", 8192)), "reasoning": False, "input": ["text"]}
    (agent_dir / "models.json").write_text(json.dumps({"providers": {"eval": {
        "baseUrl": base_url,
        "api": "openai-completions" if provider == "openai_chat" else "anthropic-messages",
        "apiKey": f"!cat {keyfile}",
        "compat": {"supportsDeveloperRole": False},
        "models": [model_entry],
    }}}, indent=2))
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(SECRET_SUFFIXES) and k != "AI_API_KEY"}
    env.update(PI_CODING_AGENT_DIR=str(agent_dir), PI_OFFLINE="1", NO_COLOR="1", TERM="dumb")
    prompt = PROMPT.format(issue=Path(args.issue_file).read_text().strip())
    cmd = [args.pi_bin, "-p", "--mode", "json", "--no-session", "--no-extensions", "--no-skills",
           "--no-prompt-templates", "--no-context-files", "--no-themes", "--offline",
           "--provider", "eval", "--model", m["name"], prompt]

    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=args.repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=open(out / "pi.stderr.log", "wb"), start_new_session=True, text=True)
    state = {"turns": 0, "stop": None, "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
             "assistant_messages": 0, "missing_usage": 0, "tool_calls": {}, "errors": []}
    events = open(out / "events.jsonl", "w")

    def reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            events.write(line.replace(key, "[REDACTED]"))
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "turn_end":
                state["turns"] += 1
            elif t == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
                msg = ev["message"]
                state["assistant_messages"] += 1
                u = msg.get("usage")
                if isinstance(u, dict):
                    for k in state["usage"]:
                        state["usage"][k] += int(u.get(k) or 0)
                else:
                    state["missing_usage"] += 1
                if msg.get("stopReason") == "error":
                    state["errors"].append(str(msg.get("errorMessage"))[:300])
            elif t == "tool_execution_start":
                n = ev.get("toolName", "?")
                state["tool_calls"][n] = state["tool_calls"].get(n, 0) + 1

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while proc.poll() is None:
        if state["turns"] >= args.max_steps:
            state["stop"] = "step_limit"
        elif time.monotonic() - t0 >= args.time_limit:
            state["stop"] = "time_limit"
        if state["stop"]:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                time.sleep(1.0)
            break
        time.sleep(0.2)
    proc.wait()
    th.join(timeout=5)
    events.close()
    keyfile.unlink(missing_ok=True)
    u = state["usage"]
    record = {
        "system": "pi", "version": "0.73.1", "exit_status": state["stop"] or f"exit {proc.returncode}",
        "error": "; ".join(state["errors"]) or None, "api_calls": state["assistant_messages"], "steps": state["turns"],
        "elapsed_s": round(time.monotonic() - t0, 3), "tool_calls": state["tool_calls"], "live": True,
        "usage": {"prompt_tokens": u["input"] + u["cacheRead"] + u["cacheWrite"], "completion_tokens": u["output"],
                  "cached_tokens": u["cacheRead"], "responses_without_usage": state["missing_usage"]},
        "model": {"provider": provider, "name": m["name"], "base_url": m["base_url"]},
    }
    (out / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
