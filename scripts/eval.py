#!/usr/bin/env python3
"""Development evaluator: run systems on the owned eval suite under matched conditions.

    scripts/eval.py --validate-suite                       # judges fail on base, pass on reference
    scripts/eval.py --partition dev --systems ours,mini    # LIVE (needs AI_API_KEY, model config)
    scripts/eval.py --partition dev --systems ours@profiles/bash-only.toml,ours --repeats 2

Systems: `ours` (default profile), `ours@<profile.toml>` (profile variant / ablation), `mini`
(pinned upstream mini-swe-agent via .venv-baseline). Every system gets a fresh copy of the task
repository, the same issue text, the same limits and the same model settings (from --profile).

Judging applies each system's final patch to a *clean* copy of the base repository, then adds the
judge-owned hidden tests and runs the task's judge command. The label is an offline development
label (`judge_pass`), not an official result. Records: <out>/results.jsonl; summary: <out>/summary.md.
Every scheduled (task, system, repeat) produces a record; failures are recorded, never dropped.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gheerefill.config import load_profile  # noqa: E402
from gheerefill.workspace import Workspace  # noqa: E402

SUITE = ROOT / "evalsuite"
GIT_ENV = {"GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@localhost", "GIT_COMMITTER_NAME": "eval",
           "GIT_COMMITTER_EMAIL": "eval@localhost", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
AUDIT_NEEDLES = ("evalsuite", "/hidden/", "hidden/test_judge", "judge_test.go", "judge.test.js", "reference/")


def load_tasks(partition: str | None, names: list[str] | None) -> list[dict[str, Any]]:
    tasks = []
    for d in sorted((SUITE / "tasks").iterdir()):
        spec = json.loads((d / "task.json").read_text())
        spec["dir"] = d
        if names and spec["task_id"] not in names:
            continue
        if partition and partition != "all" and spec["partition"] != partition:
            continue
        tasks.append(spec)
    return tasks


def fresh_repo(spec: dict[str, Any], dest: Path, overlay: Path | None = None) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(spec["dir"] / "repo", dest)
    env = {**os.environ, **GIT_ENV}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=dest, check=True, env=env, capture_output=True)
    if overlay is not None:
        shutil.copytree(overlay, dest, dirs_exist_ok=True)
    return dest


def judge(spec: dict[str, Any], repo: Path, timeout: float = 300) -> dict[str, Any]:
    for src, dst in spec["judge"]["files"].items():
        s, d = spec["dir"] / src, repo / dst
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
    t0 = time.monotonic()
    try:
        p = subprocess.run(["bash", "-c", spec["judge"]["cmd"]], cwd=repo, capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        code, out = p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        code, out = None, f"judge timed out: {e}"
    return {"judge_pass": code == 0, "exit_code": code, "duration_s": round(time.monotonic() - t0, 2),
            "output_tail": out[-1500:]}


def judge_patch(spec: dict[str, Any], patch: bytes, work: Path) -> dict[str, Any]:
    repo = fresh_repo(spec, work)
    if patch.strip():
        pf = work.parent / (work.name + ".patch")
        pf.write_bytes(patch)
        p = subprocess.run(["git", "apply", "--binary", str(pf)], cwd=repo, capture_output=True, text=True)
        if p.returncode != 0:
            return {"judge_pass": False, "exit_code": None, "apply_error": p.stderr[-800:]}
    return judge(spec, repo)


def validate_suite(tasks: list[dict[str, Any]], work: Path) -> int:
    bad = 0
    for spec in tasks:
        tool = {"javascript": "node", "go": "go"}.get(spec["language"])
        if tool and shutil.which(tool) is None:
            print(f"SKIP {spec['task_id']}: {tool} not installed")
            continue
        base = judge(spec, fresh_repo(spec, work / spec["task_id"] / "base"))
        ref = judge(spec, fresh_repo(spec, work / spec["task_id"] / "ref", overlay=spec["dir"] / "reference"))
        visible_ok = True
        ok = (not base["judge_pass"]) and ref["judge_pass"] and visible_ok
        bad += not ok
        print(f"{'OK  ' if ok else 'BAD '} {spec['task_id']:<24} base={'pass' if base['judge_pass'] else 'fail'} "
              f"reference={'pass' if ref['judge_pass'] else 'fail'}")
        if not ok:
            print("   ", (ref if not ref["judge_pass"] else base)["output_tail"][-600:].replace("\n", "\n    "))
    return 1 if bad else 0


def audit(paths: list[Path]) -> list[str]:
    hits = []
    for p in paths:
        if not p.exists():
            continue
        text = p.read_text(errors="replace")
        for needle in AUDIT_NEEDLES:
            if needle in text:
                hits.append(f"{p.name} mentions {needle!r}")
    return sorted(set(hits))


def run_ours(spec, repo, profile_path, out_dir, limits) -> dict[str, Any]:
    task = {"task_id": spec["task_id"], "repo_path": str(repo), "issue": (spec["dir"] / "issue.md").read_text(),
            "limits": limits}
    tf = out_dir / "task.json"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(json.dumps(task))
    cmd = [sys.executable, "-m", "gheerefill", "run", "--task", str(tf), "--out", str(out_dir / "runs")]
    if profile_path:
        cmd += ["--profile", str(profile_path)]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=limits["time_limit_s"] + 180)
        (out_dir / "stderr.log").write_text(p.stderr)
        recs = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        rec = recs[0] if recs else {"status": "no_output", "exit_code": p.returncode}
    except subprocess.TimeoutExpired:
        rec = {"status": "harness_timeout"}
    wall = time.monotonic() - t0
    patch = b""
    pp = (rec.get("deliverable") or {}).get("patch_path")
    if pp and Path(pp).exists():
        patch = Path(pp).read_bytes()
    run_dir = Path(rec["run_dir"]) if rec.get("run_dir") else None
    trail = [run_dir / "actions.jsonl", run_dir / "transcript.jsonl"] if run_dir else []
    usage = rec.get("usage") or {}
    return {
        "status": rec.get("status"), "termination": rec.get("termination"),
        "submission_ready": rec.get("submission_ready"), "verification": (rec.get("verification") or {}).get("status"),
        "live": (rec.get("model") or {}).get("live"), "model": rec.get("model"),
        "usage": {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                                             "total_tokens", "requests", "requests_usage_unknown", "cost_usd")},
        "steps": usage.get("steps"), "tool_calls": usage.get("tool_calls"), "timing": rec.get("timing"),
        "wall_s": round(wall, 2), "patch": patch, "trail": trail, "run_dir": str(run_dir) if run_dir else None,
        "error": rec.get("error"),
    }


def run_mini(spec, repo, profile_path, out_dir, limits) -> dict[str, Any]:
    py = ROOT / ".venv-baseline" / "bin" / "python"
    if not py.exists():
        return {"status": "baseline_not_installed", "patch": b"", "trail": [], "live": True}
    ws = Workspace(repo, out_dir / "capture")
    base = ws.init()
    cmd = [str(py), str(ROOT / "baselines" / "run_mini.py"), "--repo", str(repo), "--issue-file",
           str(spec["dir"] / "issue.md"), "--profile", str(profile_path), "--out", str(out_dir / "mini"),
           "--time-limit", str(limits["time_limit_s"]), "--max-steps", str(limits["max_steps"])]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=limits["time_limit_s"] + 180)
        (out_dir / "stderr.log").write_text(p.stderr[-20000:])
        rec = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"exit_status": f"exit {p.returncode}"}
    except subprocess.TimeoutExpired:
        rec = {"exit_status": "harness_timeout"}
    wall = time.monotonic() - t0
    final = ws.snapshot()
    u = rec.get("usage") or {}
    return {
        "status": "completed" if "error" not in rec or not rec.get("error") else "error",
        "termination": rec.get("exit_status"), "submission_ready": True, "verification": None, "live": True,
        "model": rec.get("model"),
        "usage": {"input_tokens": (u.get("prompt_tokens") or 0) - (u.get("cached_tokens") or 0),
                  "output_tokens": u.get("completion_tokens"), "cache_read_tokens": u.get("cached_tokens"),
                  "total_tokens": (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0),
                  "requests": rec.get("api_calls"), "requests_usage_unknown": u.get("responses_without_usage"),
                  "cost_usd": None},
        "steps": rec.get("steps"), "tool_calls": None, "timing": {"total_s": rec.get("elapsed_s")},
        "wall_s": round(wall, 2), "patch": ws.patch(base, final), "trail": [out_dir / "mini" / "trajectory.json"],
        "run_dir": str(out_dir), "error": rec.get("error"),
    }


def binom_two_sided(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    probs = [math.comb(n, i) / 2 ** n for i in range(n + 1)]
    return min(1.0, sum(p for p in probs if p <= probs[k] + 1e-12))


def summarize(records: list[dict[str, Any]], systems: list[str], out: Path) -> str:
    lines = ["# Evaluation summary", "", f"Records: {len(records)} (every scheduled run is listed; failures included)", ""]
    live = {r["live"] for r in records}
    lines.append(f"Model calls live: {sorted(live, key=str)} — fake/scripted runs are pipeline checks, not capability results.")
    lines.append("")
    tasks = sorted({r["task_id"] for r in records})
    lines.append("| task | " + " | ".join(systems) + " |")
    lines.append("|---|" + "---|" * len(systems))
    for t in tasks:
        row = []
        for s in systems:
            rs = [r for r in records if r["task_id"] == t and r["system"] == s]
            row.append(" ".join("PASS" if r["label"]["judge_pass"] else "fail" for r in rs) or "missing")
        lines.append(f"| {t} | " + " | ".join(row) + " |")
    lines.append("")
    lines.append("| system | judged pass | runs | submission_ready | tokens (sum) | wall s (sum) | audit flags |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in systems:
        rs = [r for r in records if r["system"] == s]
        tok = sum((r["usage"] or {}).get("total_tokens") or 0 for r in rs)
        lines.append(f"| {s} | {sum(r['label']['judge_pass'] for r in rs)} | {len(rs)} | "
                     f"{sum(bool(r['submission_ready']) for r in rs)} | {tok} | {sum(r['wall_s'] or 0 for r in rs):.0f} | "
                     f"{sum(bool(r['audit']) for r in rs)} |")
    if len(systems) >= 2:
        a = systems[0]
        lines += ["", f"Paired comparison against `{a}` (same task and repeat index):", ""]
        for b in systems[1:]:
            wins = losses = ties = 0
            for r in records:
                if r["system"] != a:
                    continue
                o = [x for x in records if x["system"] == b and x["task_id"] == r["task_id"] and x["repeat"] == r["repeat"]]
                if not o:
                    continue
                pa, pb = r["label"]["judge_pass"], o[0]["label"]["judge_pass"]
                wins += pa and not pb
                losses += pb and not pa
                ties += pa == pb
            p = binom_two_sided(min(wins, losses), wins + losses)
            lines.append(f"- `{a}` vs `{b}`: wins {wins}, losses {losses}, ties {ties}; exact sign test p={p:.3f} "
                         f"(repeats of one task are not independent tasks; small n)")
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite-validate", "--validate-suite", dest="validate", action="store_true")
    ap.add_argument("--partition", default="dev", choices=["dev", "selection", "final", "all"])
    ap.add_argument("--tasks", help="comma-separated task ids (default: whole partition)")
    ap.add_argument("--systems", default="ours,mini")
    ap.add_argument("--profile", default=str(ROOT / "profiles" / "default.toml"),
                    help="profile providing model settings for every system (variants override only their own)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--final", action="store_true", help="required to run the final (holdout) partition")
    args = ap.parse_args()
    names = args.tasks.split(",") if args.tasks else None
    out = Path(args.out or ROOT / "evals" / time.strftime("%Y%m%dT%H%M%S", time.gmtime())).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.validate:
        return validate_suite(load_tasks("all", names), out / "validate")
    if args.partition in ("final", "all") and not args.final:
        print("refusing to touch the final holdout partition without --final", file=sys.stderr)
        return 2
    tasks = load_tasks(args.partition, names)
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    if args.partition in ("final", "all"):
        with open(SUITE / "final_runs.log", "a") as fh:
            fh.write(json.dumps({"time": time.time(), "systems": systems, "profile": args.profile,
                                 "tasks": [t["task_id"] for t in tasks]}) + "\n")
    profile = load_profile(args.profile)
    manifest = {"systems": systems, "profile": args.profile, "profile_id": profile.identity(),
                "tasks": [t["task_id"] for t in tasks], "repeats": args.repeats, "partition": args.partition,
                "started_at": time.time(), "harness_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                                            capture_output=True, text=True).stdout.strip()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    records = []
    for spec in tasks:
        limits = {"time_limit_s": spec["limits"].get("time_limit_s", profile.limits.time_limit_s),
                  "max_steps": spec["limits"].get("max_steps", profile.limits.max_steps)}
        for rep in range(args.repeats):
            for system in systems:
                d = out / "work" / spec["task_id"] / f"{system.replace('/', '_').replace('@', '-')}-{rep}"
                repo = fresh_repo(spec, d / "repo")
                started = time.time()
                if system == "mini":
                    res = run_mini(spec, repo, Path(args.profile), d, limits)
                elif system.startswith("ours"):
                    prof = system.split("@", 1)[1] if "@" in system else args.profile
                    res = run_ours(spec, repo, Path(prof), d, limits)
                else:
                    print(f"unknown system {system}", file=sys.stderr)
                    return 2
                label = judge_patch(spec, res["patch"], d / "judge")
                rec = {"task_id": spec["task_id"], "partition": spec["partition"], "kind": spec["kind"],
                       "language": spec["language"], "system": system, "repeat": rep, "limits": limits,
                       "started_at": started, "ended_at": time.time(),
                       "patch_bytes": len(res["patch"]), "patch_empty": not res["patch"].strip(),
                       "label": {**label, "source": "evalsuite hidden tests (offline dev label)"},
                       "audit": audit(res.pop("trail")), **{k: v for k, v in res.items() if k != "patch"}}
                (d / "final.patch").write_bytes(res["patch"])
                records.append(rec)
                with open(out / "results.jsonl", "a") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
                print(f"{spec['task_id']:<24} {system:<28} rep={rep} judge={'PASS' if label['judge_pass'] else 'fail'} "
                      f"termination={rec.get('termination')} wall={rec['wall_s']}s", flush=True)
    print(summarize(records, systems, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
