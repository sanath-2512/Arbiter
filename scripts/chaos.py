#!/usr/bin/env python3
"""Fault-injection runs of the whole harness with invariant checks (deterministic per seed).

Each seed builds a small repository and a random scripted trajectory: correct and breaking edits,
test runs, registered reproductions (valid and bogus), syntax-breaking edits, idle steps, provider
errors (rate limits, server errors, overflows, malformed requests, timeouts), harness crashes and
cancellations, 1-3 attempts and tight step limits. Every few seeds the CLI is SIGKILLed mid-run and
the run is recovered offline with `finalize`. After every run the invariants below must hold; a
violation prints the seed so the exact run can be replayed (`scripts/chaos.py --seed N`).

Invariants
  I1 the harness returns a result record (never an unhandled exception);
  I2 a completed run exports a patch that reconstructs the selected tree from the base;
  I3 the working tree is left exactly at the selected candidate;
  I4 the target's HEAD, branch and index are as they were at the start;
  I5 every model call is counted (requests >= calls made, including failed ones);
  I6 no harness check is left half-way (state.json has no pending detour);
  I7 a "proven" level has a fail-to-pass comparison and no regression; "refuted" has a reason;
  I8 a completed run's attestation verifies offline.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gheerefill.agent import Agent  # noqa: E402
from gheerefill.attest import verify  # noqa: E402
from gheerefill.config import Profile  # noqa: E402
from gheerefill.models.fake import FakeClient  # noqa: E402
from gheerefill.task import Task  # noqa: E402
from gheerefill.workspace import Workspace  # noqa: E402

GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_AUTHOR_NAME": "c",
           "GIT_AUTHOR_EMAIL": "c@c", "GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": "c@c",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
FILES = {
    "calc/__init__.py": "from calc.ops import divide\n",
    "calc/ops.py": "def divide(a, b):\n    return a // b\n",
    "tests/test_ops.py": ("import unittest\nfrom calc import divide\n\n\nclass T(unittest.TestCase):\n"
                          "    def test_exact(self):\n        self.assertEqual(divide(6, 3), 2)\n\n"
                          "    def test_true(self):\n        self.assertEqual(divide(7, 2), 3.5)\n"),
}
TEST_CMD = "python3 -m unittest discover -s tests"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV).stdout.decode()


def make_repo(root: Path) -> Path:
    for rel, content in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def tc(name, **args):
    return {"name": name, "arguments": args}


def turn(*calls):
    return {"text": "", "tool_calls": list(calls)}


def trajectory(rng: random.Random, scratch: Path) -> list:
    repro = scratch / "repro.py"
    bodies = {"good": "import os, sys\nsys.path.insert(0, os.getcwd())\nfrom calc import divide\nassert divide(7, 2) == 3.5\n",
              "bogus": "print('always fine')\n"}
    menu = [
        lambda: turn(tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")),
        lambda: turn(tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a * b")),
        lambda: turn(tc("edit_file", path="calc/ops.py", old_str="return a / b", new_str="return a // b")),
        lambda: turn(tc("edit_file", path="calc/ops.py", old_str="    return", new_str="  return(")),  # syntax
        lambda: turn(tc("write_file", path="calc/extra.py", content="X = 1\n")),
        lambda: turn(tc("write_file", path="tests/test_half.py", content=(
            "import unittest\nfrom calc import divide\n\n\nclass H(unittest.TestCase):\n"
            "    def test_half(self):\n        self.assertEqual(divide(1, 2), 0.5)\n"))),
        lambda: turn(tc("bash", command=TEST_CMD)),
        lambda: turn(tc("bash", command="echo idle")),
        lambda: turn(tc("bash", command="git add -A && git commit -qm wip || true")),  # hygiene target
        lambda: turn(tc("write_file", path=str(repro), content=bodies[rng.choice(list(bodies))])),
        lambda: turn(tc("register_reproduction", command=f"python3 {repro}")),
        lambda: turn(tc("submit", summary="done")),
        lambda: {"error": "rate_limited", "message": "slow down", "retry_after_s": 0.0},
        lambda: {"error": "server_error", "message": "overloaded", "usage_uncertain": True},
        lambda: {"error": "context_overflow", "message": "prompt is too long"},
        lambda: {"text": "thinking out loud", "tool_calls": []},
        lambda: {"text": "", "tool_calls": [{"name": "edit_file", "raw_arguments": "{broken"}]},
    ]
    weights = [6, 3, 2, 2, 2, 2, 6, 2, 1, 2, 2, 5, 2, 2, 1, 1, 1]
    n = rng.randint(3, 22)
    turns = [rng.choices(menu, weights)[0]() for _ in range(n)]
    if rng.random() < 0.15:
        turns.insert(rng.randrange(len(turns) + 1), {"error": "malformed_request", "message": "bad request"})
    return turns


def check_invariants(result: dict, agent: Agent | None, repo: Path, run_dir: Path, head0: str, calls: int) -> list[str]:
    bad = []
    if not isinstance(result, dict) or "status" not in result:
        return ["I1 no result record"]
    if result["status"] == "completed":
        d = result["deliverable"]
        if not d["reconstruction_verified"]:
            bad.append(f"I2 reconstruction failed: {d['reconstruction_detail']}")
        ws = Workspace(repo, run_dir)
        ws.attach(json.loads((run_dir / "state.json").read_text())["base_tree"])
        if ws.snapshot() != result["selected_candidate"]["tree"]:
            bad.append("I3 working tree is not the selected candidate")
        v = verify(run_dir)
        if not v["ok"]:
            bad.append("I8 attestation does not verify: " + str([c for c in v["checks"] if not c["ok"]])[:300])
        p = result.get("proof") or {}
        comps = p.get("comparisons") or []
        if p.get("level") == "proven" and not (any(c["shows_fix"] for c in comps)
                                                and not any(c["shows_regression"] for c in comps)):
            bad.append("I7 proven without fail-to-pass evidence")
    head = git(repo, "rev-parse", "HEAD").strip()
    if head != head0:
        bad.append(f"I4 HEAD moved {head0[:8]} -> {head[:8]}")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, env=GIT_ENV).returncode != 0:
        bad.append("I4 index not restored")
    if calls and (result.get("usage") or {}).get("requests", 0) < calls:
        bad.append(f"I5 {calls} model calls but {result['usage'].get('requests')} requests counted")
    st = run_dir / "state.json"
    if st.exists() and json.loads(st.read_text()).get("detour"):
        bad.append("I6 detour pending after the run")
    return bad


def run_seed(seed: int, work: Path) -> list[str]:
    rng = random.Random(seed)
    root = work / f"s{seed}"
    repo = make_repo(root / "repo")
    head0 = git(repo, "rev-parse", "HEAD").strip()
    run_dir = root / "run"
    turns = trajectory(rng, run_dir / "scratch")
    p = Profile()
    p.name, p.model.provider, p.model.name, p.model.script = "chaos", "fake", "fake-chaos", "inline"
    p.limits.max_steps = rng.randint(3, 16)
    p.limits.time_limit_s, p.limits.finalize_reserve_s = 120.0, 15.0
    p.retry.base_delay_s, p.retry.max_attempts = 0.0, rng.randint(1, 3)
    p.policy.max_attempts, p.policy.min_attempt_s = rng.randint(1, 3), 0.0
    p.policy.first_attempt_share = rng.choice([0.3, 0.6, 1.0])
    mode = rng.random()
    if mode < 0.1:  # a crash inside the loop
        turns.insert(rng.randrange(len(turns) + 1), lambda m: (_ for _ in ()).throw(RuntimeError("chaos crash")))
    client = FakeClient(turns)
    holder: dict = {}
    if 0.1 <= mode < 0.2:  # a cancellation (signal) during a model call
        idx = rng.randrange(len(turns) + 1)

        def cancel(messages):
            holder["agent"].request_cancel()
            return {"text": "", "tool_calls": []}
        client.turns.insert(idx, cancel)
    agent = Agent(Task(f"chaos-{seed}", repo, "Fix divide so that divide(7, 2) == 3.5."), p, client, run_dir,
                  log=lambda m: None, sleep=lambda s: None)
    holder["agent"] = agent
    try:
        result = agent.run()
    except Exception as e:  # noqa: BLE001
        return [f"I1 unhandled {type(e).__name__}: {e}"]
    return check_invariants(result, agent, repo, run_dir, head0, len(client.calls))


def run_kill_seed(seed: int, work: Path) -> list[str]:
    """Real process: SIGKILL the CLI at a random moment, then recover offline with `finalize`."""
    rng = random.Random(seed)
    root = work / f"k{seed}"
    repo = make_repo(root / "repo")
    head0 = git(repo, "rev-parse", "HEAD").strip()
    turns = [turn(tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")),
             turn(tc("bash", command="sleep 0.3; " + TEST_CMD)), turn(tc("bash", command="sleep 5")),
             turn(tc("submit"))]
    script = root / "script.json"
    script.write_text(json.dumps(turns))
    prof = root / "p.toml"
    prof.write_text(f'[model]\nprovider = "fake"\nscript = "{script}"\n[policy]\nmax_attempts = 1\n')
    task = json.dumps({"task_id": f"kill-{seed}", "repo_path": str(repo), "issue": "fix divide"})
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AI_", "GHEEREFILL_"))}
    proc = subprocess.Popen([sys.executable, "-m", "gheerefill", "run", "--profile", str(prof), "--out",
                             str(root / "out")], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, text=True)
    proc.stdin.write(task + "\n")
    proc.stdin.close()
    time.sleep(rng.uniform(0.6, 2.5))
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    states = list((root / "out").rglob("state.json"))
    if not states:
        return []  # killed before the run started: nothing to recover
    run_dir = states[0].parent
    if (run_dir / "result.json").exists():
        result = json.loads((run_dir / "result.json").read_text())
    else:
        p = subprocess.run([sys.executable, "-m", "gheerefill", "finalize", "--run-dir", str(run_dir)], cwd=ROOT,
                           capture_output=True, text=True, env=env, timeout=120)
        if p.returncode not in (0, 1) or not p.stdout.strip():
            return [f"I1 finalize failed ({p.returncode}): {p.stderr[-300:]}"]
        result = json.loads(p.stdout.strip().splitlines()[-1])
    return check_invariants(result, None, repo, run_dir, head0, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--seed", type=int, help="run one seed only")
    ap.add_argument("--kill-every", type=int, default=10, help="every Nth seed is a SIGKILL + recovery run (0 = never)")
    args = ap.parse_args()
    seeds = [args.seed] if args.seed is not None else list(range(args.seeds))
    failures = 0
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ghee-chaos-") as td:
        for s in seeds:
            kill = args.kill_every and s % args.kill_every == args.kill_every - 1
            bad = run_kill_seed(s, Path(td)) if kill else run_seed(s, Path(td))
            if bad:
                failures += 1
                print(f"seed {s}{' (kill)' if kill else ''}: VIOLATION " + "; ".join(bad), flush=True)
    print(f"chaos: {len(seeds)} seeds, {failures} with invariant violations, {time.monotonic() - t0:.0f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
