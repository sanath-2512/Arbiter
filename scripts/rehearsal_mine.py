#!/usr/bin/env python3
"""Build rehearsal tasks from real historical fixes (dev only).

    python scripts/rehearsal_mine.py list REPO_KEY [--since 2025-01-01] [--limit 40]
    python scripts/rehearsal_mine.py make REPO_KEY FIX_SHA --id ID --type TYPE --size SIZE --issue FILE
    python scripts/rehearsal_mine.py remake TASK_ID [--run-tests A,B]   # rebuild from task.json (e.g. wider P2P)

`list` shows non-merge commits that change both tests and source with a small source diff.
`make` turns one into a task: base = the fix's parent; the fix's test changes become the hidden test
patch and its other changes the reference patch; FAIL_TO_PASS / PASS_TO_PASS are derived by running
the touched tests before and after the fix (SWE-bench's method). The issue text is written by hand
from the fix's symptoms, never from the diff's wording; the label is stored encoded and is judge-only.

A behaviour-preserving refactoring usually has no upstream test that fails before it. For those,
`--extra-test REPO_PATH=FILE` adds a lab-authored test for the API the issue asks for (as a new file in
the hidden test patch); the task records which tests are lab-authored.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts import rehearsal as R  # noqa: E402
from arbiter.proof import is_test_path  # noqa: E402

RUNNERS = {
    # one test module that fails to import must not abort the session and mask every other module
    "pytest": "python -m pytest -p no:cacheprovider -rA -q --no-header --continue-on-collection-errors -o addopts='' "
              "{tests}",
    "django": "python tests/runtests.py --verbosity 2 --parallel 1 {tests}",
    "go": "go test -count=1 -v {tests}",
    "mocha": "npx mocha --reporter json {tests}",
}


def mirror(key: str) -> Path:
    return R.mirror_path(R.load_json(R.MANIFEST)[key])


def gitm(key: str, *args: str) -> str:
    return subprocess.run(["git", "--git-dir", str(mirror(key)), *args], capture_output=True, text=True,
                          check=True).stdout


def cmd_list(key: str, since: str, limit: int) -> None:
    log = gitm(key, "log", "--no-merges", f"--since={since}", "--format=@@@%H%x09%ad%x09%s", "--date=short",
               "--numstat", "HEAD")
    shown = 0
    for block in log.split("@@@")[1:]:
        lines = [l for l in block.splitlines() if l.strip()]
        head = lines[0].split("\t", 2)
        if len(head) < 3:
            continue
        sha, date, subject = head[0], head[1], head[2]
        stats = [l.split("\t") for l in lines[1:] if l.count("\t") == 2]
        src = [(a, d, p) for a, d, p in stats if not is_test_path(p) and not p.endswith((".md", ".rst", ".txt"))
               and a.isdigit()]
        tests = [p for a, d, p in stats if is_test_path(p)]
        churn = sum(int(a) + int(d) for a, d, _ in src)
        if tests and src and len(src) <= 3 and churn <= 120:
            print(f"{sha[:12]} {date} src={len(src)}:{churn:<4} tests={len(tests)} {subject[:90]}")
            print("      " + " ".join(p for _, _, p in src)[:160])
            shown += 1
            if shown >= limit:
                break


def new_file_patch(path: str, text: str) -> str:
    lines = text.splitlines()
    body = "".join(f"+{l}\n" for l in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def cmd_make(key: str, fix: str, task_id: str, kind: str, size: str, issue_file: Path, runner: str,
             limits: dict, depth: int, run_tests: list[str] | None, overlay: dict | None,
             extra_tests: dict[str, str] | None = None) -> None:
    manifest = R.load_json(R.MANIFEST)
    repo = manifest[key]
    fix = gitm(key, "rev-parse", fix).strip()
    base = gitm(key, "rev-parse", f"{fix}^").strip()
    files = [l.split("\t") for l in gitm(key, "diff", "--name-status", "--no-renames", base, fix).splitlines()]
    test_files = [p for _, p in files if is_test_path(p)]
    src_files = [p for _, p in files if not is_test_path(p)]
    diff = lambda paths: gitm(key, "diff", "--binary", "--full-index", "--no-renames", base, fix, "--", *paths) \
        if paths else ""  # noqa: E731
    test_patch, reference = diff(test_files), diff(src_files)
    for path, text in (extra_tests or {}).items():
        if path in test_files or not is_test_path(path):
            raise SystemExit(f"--extra-test {path}: must be a new test file")
        test_patch += new_file_patch(path, text)
        test_files.append(path)
    task = {"task_id": task_id, "repo": key, "base_commit": base, "type": kind, "size_class": size,
            "issue": issue_file.read_text().strip(), "limits": limits, "history_depth": depth,
            "source": {"kind": "historical-fix", "fix_commit": fix[:12],
                       "note": "issue text written by hand from the fix's observable symptoms"}}
    if overlay:
        task["overlay"] = overlay
    if extra_tests:
        task["source"]["lab_authored_tests"] = sorted(extra_tests)
    tests = run_tests or derive_run_tests(runner, test_files)
    verify = {"runner": runner, "cmd": repo.get("test_cmd") or RUNNERS[runner], "run_tests": tests, "fail_to_pass": [],
              "pass_to_pass": []}
    # derive F2P / P2P by running the touched tests before and after the fix
    work = R.WORK / f"make-{task_id}-{uuid.uuid4().hex[:6]}"
    task_obj = {**task, "dir": None}
    with R.env_lock(key):
        tenv = R.tool_environment(R.ensure_env(key, repo))
        b = R.build_base(task_obj, repo, work / "b")
        R.prepare_repo(b, repo, tenv, print)
        R.restore_and_apply_tests(b, test_patch)
        before = R.parse_statuses(runner, run(b, verify, tenv))
        R.git(b, "apply", "--whitespace=nowarn", "-", input=reference.encode())
        R.prepare_repo(b, repo, tenv, print)
        after = R.parse_statuses(runner, run(b, verify, tenv))
    verify["fail_to_pass"] = sorted(t for t, s in after.items() if s == "pass" and before.get(t) != "pass")
    verify["pass_to_pass"] = sorted(t for t, s in after.items() if s == "pass" and before.get(t) == "pass")
    shutil.rmtree(work, ignore_errors=True)
    if not verify["fail_to_pass"]:
        raise SystemExit(f"no FAIL_TO_PASS tests found (before={len(before)}, after={len(after)}); not a usable task")
    gold = gold_files(src_files)
    label = {"fix_commit": fix, "test_patch": test_patch, "reference_patch": reference, "verify": verify,
             "gold_files": gold}
    d = R.TASKS / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(json.dumps(task, indent=2) + "\n")
    (d / "label.b64").write_text(R.encode_label(label))
    print(f"{task_id}: base {base[:12]}, F2P {len(verify['fail_to_pass'])}, P2P {len(verify['pass_to_pass'])}, "
          f"gold {gold}")


NOT_GOLD = re.compile(r"(^|/)(CHANGES|CHANGELOG|NEWS|HISTORY|AUTHORS|\.mailmap)[^/]*$|(^|/)changelogs?/|"
                      r"\.(md|rst|txt)$|^docs?/", re.I)


def gold_files(src_files: list[str]) -> list[str]:
    """Files a fix location may be scored against: source, not docs, changelog fragments or metadata."""
    return [p for p in src_files if not NOT_GOLD.search(p)]


def cmd_remake(task_id: str, run_tests: list[str] | None) -> None:
    """Rebuild a task from its own task.json (same fix, issue, overlay, lab-authored tests)."""
    d = R.TASKS / task_id
    task = json.loads((d / "task.json").read_text())
    label = R.decode_label((d / "label.b64").read_text())
    extra = {}
    for path in task["source"].get("lab_authored_tests", []):
        m = re.search(rf"^diff --git a/{re.escape(path)} b/.*?@@ -0,0 \+1,\d+ @@\n(.*?)(?=^diff --git |\Z)",
                      label["test_patch"], re.M | re.S)
        extra[path] = "".join(l[1:] + "\n" for l in m.group(1).splitlines()) if m else ""
    issue = R.WORK / f"remake-{task_id}.md"
    issue.parent.mkdir(parents=True, exist_ok=True)
    issue.write_text(task["issue"] + "\n")
    cmd_make(task["repo"], label["fix_commit"], task_id, task["type"], task["size_class"], issue,
             label["verify"]["runner"], task["limits"], task.get("history_depth", 200),
             run_tests or label["verify"]["run_tests"], task.get("overlay"), extra or None)


def derive_run_tests(runner: str, test_files: list[str]) -> list[str]:
    if runner == "django":
        return sorted({".".join(Path(p).with_suffix("").parts[1:]) for p in test_files if p.startswith("tests/")
                       and p.endswith(".py")})
    if runner == "go":
        return sorted({"./" + str(Path(p).parent) for p in test_files if p.endswith("_test.go")})
    return sorted(p for p in test_files if p.endswith((".py", ".js", ".ts")))


def run(repo_dir: Path, verify: dict, tenv: dict) -> str:
    cmd = verify["cmd"].replace("{tests}", " ".join(verify["run_tests"]))
    p = subprocess.run(["bash", "-c", cmd], cwd=repo_dir, env=tenv, capture_output=True, text=True, timeout=3600)
    return p.stdout + "\n" + p.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list")
    l.add_argument("repo")
    l.add_argument("--since", default="2025-01-01")
    l.add_argument("--limit", type=int, default=40)
    m = sub.add_parser("make")
    m.add_argument("repo")
    m.add_argument("fix")
    m.add_argument("--id", required=True)
    m.add_argument("--type", required=True)
    m.add_argument("--size", required=True)
    m.add_argument("--issue", required=True)
    m.add_argument("--runner", default="pytest")
    m.add_argument("--time-limit", type=float, default=1200)
    m.add_argument("--max-steps", type=int, default=100)
    m.add_argument("--depth", type=int, default=200)
    m.add_argument("--run-tests", help="comma-separated test targets (default: derived from the fix's test files)")
    m.add_argument("--overlay", help="JSON overlay spec (pathological tasks)")
    m.add_argument("--extra-test", action="append", default=[], metavar="REPO_PATH=FILE",
                   help="lab-authored hidden test added as a new file (refactorings without an upstream F2P test)")
    r = sub.add_parser("remake")
    r.add_argument("task_id")
    r.add_argument("--run-tests")
    a = ap.parse_args()
    if a.cmd == "list":
        cmd_list(a.repo, a.since, a.limit)
    elif a.cmd == "remake":
        cmd_remake(a.task_id, a.run_tests.split(",") if a.run_tests else None)
    else:
        cmd_make(a.repo, a.fix, a.id, a.type, a.size, Path(a.issue), a.runner,
                 {"time_limit_s": a.time_limit, "max_steps": a.max_steps}, a.depth,
                 a.run_tests.split(",") if a.run_tests else None, json.loads(a.overlay) if a.overlay else None,
                 {k: Path(v).read_text() for k, v in (e.split("=", 1) for e in a.extra_test)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
