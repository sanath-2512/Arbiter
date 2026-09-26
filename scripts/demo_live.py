#!/usr/bin/env python3
"""LIVE demonstration (requires AI_API_KEY and the model configuration; makes paid calls).

    python scripts/demo_live.py [--task py-slugify] [--recovery-task js-duration]

Part 1: a normal run on a real task. Shows the repository investigation, the edits, the executed
checks and their classified evidence, the exact exported patch with its clean-reconstruction
check, and an offline judge label from the suite's hidden tests.
Part 2: a supported recovery behaviour on a live run. The harness process is hard-killed
(SIGKILL, no chance to clean up) once it has produced a candidate. `gheerefill finalize` then
rebuilds the deliverable from the checkpoint without any model call, and the recovered patch is judged.

Only dev-partition tasks should be used here (the demo must not consume the holdout).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval as ev  # noqa: E402  (scripts/eval.py)


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}", flush=True)


def show_run(run_dir: Path, spec: dict) -> None:
    result = json.loads((run_dir / "result.json").read_text())
    actions = [json.loads(l) for l in (run_dir / "actions.jsonl").read_text().splitlines()] \
        if (run_dir / "actions.jsonl").exists() else []
    print(f"model: {result['model']['provider']} / {result['model']['name']} (live={result['model']['live']})")
    print(f"termination: {result['termination']} · steps: {result['usage']['steps']} · "
          f"tokens: {result['usage']['total_tokens']} · requests: {result['usage']['requests']} · "
          f"wall: {result['timing']['total_s']}s")
    print("\nactions:")
    for a in actions:
        arg = json.loads(a["arguments"]) if a["arguments"].startswith("{") else {}
        detail = arg.get("command") or arg.get("path") or arg.get("pattern") or ""
        print(f"  step {a['step']:>2}  {a['tool']:<11} {str(detail)[:70]!s:<70} {a['status']}")
    print("\nverification evidence (bound to exact candidate trees):")
    for e in (json.loads(l) for l in (run_dir / "evidence.jsonl").read_text().splitlines()) \
            if (run_dir / "evidence.jsonl").exists() else []:
        print(f"  {e['id']:<4} tree {e['tree'][:10]}  {e['outcome']:<17} {e['counts']}  {e['command'][:50]}  ({e['source']})")
    d = result.get("deliverable") or {}
    print(f"\nselected candidate: {result['selected_candidate']['tree'][:12]} — {result['selected_candidate']['reason']}")
    print(f"verification status: {result['verification']['status']} — {result['verification']['detail']}")
    print(f"submission_ready: {result['submission_ready']} · reconstruction: {d.get('reconstruction_detail')}")
    print(f"\npatch ({d.get('shortstat')}), sha256 {d.get('patch_sha256', '')[:16]}:\n")
    print(Path(d["patch_path"]).read_text() if d.get("patch_path") else "(none)")
    label = ev.judge_patch(spec, Path(d["patch_path"]).read_bytes() if d.get("patch_path") else b"",
                           run_dir.parent.parent.parent / "judge")
    print(f"offline judge (hidden tests, dev label — not an official result): {'PASS' if label['judge_pass'] else 'FAIL'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="py-slugify")
    ap.add_argument("--recovery-task", default="js-duration")
    ap.add_argument("--profile", default=str(ROOT / "profiles" / "default.toml"))
    ap.add_argument("--out", default=str(ROOT / "runs" / "demo-live"))
    args = ap.parse_args()
    py = sys.executable
    chk = subprocess.run([py, "-m", "gheerefill", "check-config", "--profile", args.profile], cwd=ROOT,
                         capture_output=True, text=True)
    if chk.returncode != 0:
        print(chk.stderr.strip(), file=sys.stderr)
        print("demo_live needs a real model configuration and AI_API_KEY; refusing to substitute a stub.", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    tasks = {t["task_id"]: t for t in ev.load_tasks("dev", None)}

    banner(f"PART 1 — live run on task {args.task}")
    spec = tasks[args.task]
    base = out / args.task
    repo = ev.fresh_repo(spec, base / "repo")
    tf = base / "task.json"
    tf.write_text(json.dumps({"task_id": spec["task_id"], "repo_path": str(repo),
                              "issue": (spec["dir"] / "issue.md").read_text(), "limits": spec["limits"]}))
    print((spec["dir"] / "issue.md").read_text())
    p = subprocess.run([py, "-m", "gheerefill", "run", "--task", str(tf), "--profile", args.profile,
                        "--out", str(base / "runs")], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    rec = json.loads(p.stdout.strip().splitlines()[-1])
    show_run(Path(rec["run_dir"]), spec)

    banner(f"PART 2 — hard kill (SIGKILL) during a live run on {args.recovery_task}, then offline recovery")
    spec = tasks[args.recovery_task]
    base = out / args.recovery_task
    repo = ev.fresh_repo(spec, base / "repo")
    tf = base / "task.json"
    tf.write_text(json.dumps({"task_id": spec["task_id"], "repo_path": str(repo),
                              "issue": (spec["dir"] / "issue.md").read_text(), "limits": spec["limits"]}))
    proc = subprocess.Popen([py, "-m", "gheerefill", "run", "--task", str(tf), "--profile", args.profile,
                             "--out", str(base / "runs")], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    run_dir = None
    deadline = time.time() + float(spec["limits"]["time_limit_s"])
    while time.time() < deadline and proc.poll() is None:
        cands = list((base / "runs").rglob("candidates.json"))
        if cands and len([c for c in json.loads(cands[0].read_text()) if not c["is_base"]]) >= 1:
            run_dir = cands[0].parent
            break
        time.sleep(0.5)
    if proc.poll() is not None or run_dir is None:
        print("the run finished (or produced no candidate) before the kill point; nothing to recover")
        return 1
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    print(f"SIGKILLed harness process {proc.pid}; result.json present: {(run_dir / 'result.json').exists()}")
    print(f"working tree right now (possibly mid-edit):\n{subprocess.run(['git', 'status', '--short'], cwd=repo, capture_output=True, text=True).stdout}")
    fin = subprocess.run([py, "-m", "gheerefill", "finalize", "--run-dir", str(run_dir)], cwd=ROOT,
                         capture_output=True, text=True)
    print(fin.stderr[-1500:])
    show_run(run_dir, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
