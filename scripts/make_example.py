#!/usr/bin/env python3
"""Prepare disposable git working copies of example repositories and write task JSON.

    python scripts/make_example.py calc-divide [more names] --work work/ > tasks.jsonl

Each example is copied to <work>/<name>/repo, initialised as a git repository with one
commit, and a task object (local development protocol) is printed as one JSON line.
Existing copies are replaced (they are disposable by construction).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "examples" / "tasks"


def prepare(name: str, work: Path) -> dict:
    spec_path = TASKS / f"{name}.json"
    spec = json.loads(spec_path.read_text())
    template = (spec_path.parent / spec["template_repo"]).resolve()
    dest = (work / name / "repo").resolve()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = {"GIT_AUTHOR_NAME": "example", "GIT_AUTHOR_EMAIL": "example@localhost",
           "GIT_COMMITTER_NAME": "example", "GIT_COMMITTER_EMAIL": "example@localhost",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    for cmd in (["git", "init", "-q"], ["git", "symbolic-ref", "HEAD", "refs/heads/main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=dest, check=True, env=env)
    issue = (spec_path.parent / spec["issue_file"]).read_text()
    return {"task_id": spec["task_id"], "repo_path": str(dest), "issue": issue, "limits": spec.get("limits", {})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="example names (default: all)")
    ap.add_argument("--work", default=str(ROOT / "work"))
    args = ap.parse_args()
    names = args.names or sorted(p.stem for p in TASKS.glob("*.json"))
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    for n in names:
        sys.stdout.write(json.dumps(prepare(n, work)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
