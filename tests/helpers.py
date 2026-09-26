"""Shared fixtures for deterministic tests. All repositories are disposable temp dirs."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gheerefill.agent import Agent
from gheerefill.config import Profile
from gheerefill.models.fake import FakeClient
from gheerefill.task import Task

ROOT = Path(__file__).resolve().parent.parent
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


# Tests start `make` themselves; when the suite itself runs under `make -C DIR test`, make's own
# variables (MAKEFLAGS carries -w) would make those child makes print "Entering directory" lines
# into the stdout the tests parse.
for _var in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"):
    os.environ.pop(_var, None)

def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV).stdout.decode()


def make_repo(root: Path, files: dict[str, Any], *, init_git: bool = True, commit: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    if init_git:
        git(root, "init", "-q")
        git(root, "symbolic-ref", "HEAD", "refs/heads/main")  # `init -b` needs git >= 2.28
        if commit:
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "base")
    return root


CALC = {
    "calc/__init__.py": "from calc.ops import divide\n",
    "calc/ops.py": "def divide(a, b):\n    return a // b\n",
    "tests/test_ops.py": (
        "import unittest\nfrom calc import divide\n\n\nclass T(unittest.TestCase):\n"
        "    def test_exact(self):\n        self.assertEqual(divide(6, 3), 2)\n\n"
        "    def test_true(self):\n        self.assertEqual(divide(7, 2), 3.5)\n"
    ),
}
TEST_CMD = "python3 -m unittest discover -s tests"


def tc(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def turn(*calls: dict[str, Any], text: str = "") -> dict[str, Any]:
    return {"text": text, "tool_calls": list(calls)}


def test_profile(**limits: Any) -> Profile:
    p = Profile()
    p.name = "test"
    p.model.provider = "fake"
    p.model.name = "fake-scripted"
    p.model.script = "inline"
    p.limits.time_limit_s = limits.pop("time_limit_s", 120.0)
    p.limits.max_steps = limits.pop("max_steps", 30)
    p.limits.finalize_reserve_s = limits.pop("finalize_reserve_s", 15.0)
    for k, v in limits.items():
        setattr(p.limits, k, v)
    p.retry.base_delay_s = 0.0
    p.policy.max_attempts = 1  # single-attempt semantics unless a test enables adaptive attempts
    return p


def run_agent(repo: Path, turns: list, run_dir: Path, *, profile: Profile | None = None,
              issue: str = "Fix divide so that divide(7, 2) == 3.5.", env: dict[str, str] | None = None,
              clock=None, **kw) -> tuple[dict[str, Any], Agent]:
    profile = profile or test_profile()
    client = FakeClient(turns)
    extra = {"clock": clock} if clock else {}
    agent = Agent(Task("t1", repo, issue), profile, client, run_dir,
                  env=env if env is not None else dict(os.environ), log=lambda m: None, sleep=lambda s: None,
                  **extra, **kw)
    return agent.run(), agent


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ghee-test-")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()
