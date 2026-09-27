"""Repository notes carried between runs (a small, safe learning loop).

Only facts the harness observed by execution are kept: test commands that ran to a recognised
runner summary, and install commands that exited 0. Never code, patches, issue text or model
prose, so nothing learned on one issue can leak a solution into another, and a wrong belief of
the model cannot be "learned". Keyed by the repository's origin URL (or its path), stored under
the run-records root (`runs/.memory/`), shown to the next run on the same repository as hints.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from arbiter.records import atomic_write_json

INSTALL_RE = re.compile(r"^\s*(?:(?:pip3?|python3?\s+-m\s+pip|uv\s+pip)\s+install|uv\s+sync|poetry\s+install|"
                        r"pipenv\s+install|(?:npm|pnpm)\s+(?:ci|install)|yarn(?:\s+install)?$|bundle\s+install|"
                        r"go\s+mod\s+download|composer\s+install|cargo\s+fetch)\b")
MAX_CHECKS, MAX_SETUP = 8, 5


def repo_key(repo: Path) -> str:
    cfg = repo / ".git" / "config"
    ident = str(repo.resolve())
    try:
        m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', cfg.read_text(errors="replace"))
        if m:
            ident = re.sub(r"(\.git)?/?$", "", m.group(1).lower())
    except OSError:
        pass
    return hashlib.sha256(ident.encode()).hexdigest()[:16]


def load(root: Path, repo: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / f"{repo_key(repo)}.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def render(notes: dict[str, Any]) -> str:
    checks, setup = notes.get("checks") or [], notes.get("setup") or []
    if not checks and not setup:
        return ""
    lines = [f"\nFrom {notes.get('runs', 1)} earlier run(s) on this repository (commands the harness saw run; the "
             "environment may have changed since):"]
    for c in checks[:MAX_CHECKS]:
        lines.append(f"- `{c['command']}` ran ({c['runner']}, {c['tests']} tests, {c['duration_s']} s)")
    for s in setup[:MAX_SETUP]:
        lines.append(f"- `{s}` completed successfully")
    return "\n".join(lines)


def update(root: Path, repo: Path, records: list, setup_commands: list[str], redact) -> None:
    notes = load(root, repo)
    checks = {c["command"]: c for c in notes.get("checks") or []}
    for r in records:
        if r.source != "agent" or r.kind != "check" or r.outcome not in ("passed", "failed") or r.binding != "exact":
            continue
        cmd = redact(r.check_key)[:300]
        checks.pop(cmd, None)
        checks[cmd] = {"command": cmd, "runner": r.runner, "tests": sum(int(v) for v in r.counts.values()),
                       "duration_s": round(r.duration_s, 1)}
    setup = [s for s in notes.get("setup") or [] if s not in setup_commands] + \
        [redact(s)[:300] for s in setup_commands]
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / f"{repo_key(repo)}.json", {
        "schema": "arbiter.memory/v1", "runs": int(notes.get("runs") or 0) + 1, "updated_at": time.time(),
        "checks": list(checks.values())[-MAX_CHECKS:], "setup": list(dict.fromkeys(setup))[-MAX_SETUP:],
    })
