#!/usr/bin/env python3
"""Judge Rehearsal Lab: run gheerefill the way a judge would, on pinned real repositories.

    python scripts/rehearsal.py validate [--tasks a,b]          # labels are sound (base fails, fix passes)
    python scripts/rehearsal.py run --task rehearsal/tasks/ID [--config F] [--policy NAME] [--faults JSON]
                                    [--signal TERM@30|KILL@30] [--repeat N]
    python scripts/rehearsal.py gauntlet [--configs A,B,C,D,E,F] [--tasks ...] [--repeats N] [--policy NAME]
    python scripts/rehearsal.py localize                       # deterministic localisation recall@k
    python scripts/rehearsal.py report RESULTS.jsonl            # tables from recorded runs

One run is: clean base -> launch gheerefill (no TTY, issue supplied like a judge would) -> wait ->
collect the patch -> apply it to a *fresh* clean base -> run the hidden verifier -> record. The
working directory the harness left behind is never inspected: a solved-looking tree whose exported
patch does not reconstruct is a failed task.

Evaluation boundary. The runtime (gheerefill/) never reads rehearsal/ (enforced by a test). Judge
data (hidden tests, reference patches, verify commands) lives in rehearsal/tasks/<id>/label.b64,
gzip+base64 so a broad grep cannot hit it, decoded only after the harness has exited. Task
repositories are built outside the harness checkout, with history up to the base commit only (no
future commits, no remote). Every run's transcript is audited for references to the lab. The mirrors
in rehearsal/repos/ hold full history, so the audit also flags them; for a hard boundary run the lab
in a container that mounts only the task repository.

Records never claim capability for scripted policies: `model_kind` is "live" or "scripted-policy".
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import gzip
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REH = ROOT / "rehearsal"
TASKS = REH / "tasks"
MIRRORS = REH / "repos"
ENVS = REH / "envs"
RESULTS = REH / "results"
MANIFEST = REH / "manifests" / "repos.json"
CONFIGS = REH / "manifests" / "configs.json"
WORK = Path(os.environ.get("GHEEREFILL_REHEARSAL_WORK") or Path(tempfile.gettempdir()) / "ghee-rehearsal")
GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_AUTHOR_NAME": "rehearsal",
           "GIT_AUTHOR_EMAIL": "rehearsal@localhost", "GIT_COMMITTER_NAME": "rehearsal",
           "GIT_COMMITTER_EMAIL": "rehearsal@localhost", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0"}
sys.path.insert(0, str(ROOT))


# ----------------------------------------------------------------------------- labels (judge-only)
def encode_label(obj: dict) -> str:
    raw = gzip.compress(json.dumps(obj, sort_keys=True).encode(), mtime=0)
    b = base64.b64encode(raw).decode()
    return "\n".join(b[i:i + 100] for i in range(0, len(b), 100)) + "\n"


def decode_label(text: str) -> dict:
    return json.loads(gzip.decompress(base64.b64decode("".join(text.split()))))


def read_label(task_dir: Path) -> dict:
    return decode_label((task_dir / "label.b64").read_text())


def load_json(p: Path) -> Any:
    return json.loads(p.read_text())


def load_task(task_dir: Path) -> dict:
    t = load_json(task_dir / "task.json")
    t["dir"] = task_dir
    return t


def all_tasks(names: list[str] | None = None) -> list[dict]:
    out = []
    for d in sorted(p for p in TASKS.iterdir() if (p / "task.json").exists()):
        if names is None or d.name in names:
            out.append(load_task(d))
    return out


# ----------------------------------------------------------------------------- git / repositories
def git(cwd: Path, *args: str, check: bool = True, input: bytes | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=cwd, env={**GIT_ENV, **(env or {})}, capture_output=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed in {cwd}: {p.stderr.decode(errors='replace')[:400]}")
    return p


def mirror_path(repo: dict) -> Path:
    path = MIRRORS / repo["mirror"]
    if not path.exists():
        MIRRORS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", repo["url"], str(path)], check=True, env=GIT_ENV)
    return path


def build_base(task: dict, repo: dict, dest: Path) -> Path:
    """A fresh repository at the task's base commit: history up to base only, no remote, overlay applied."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git(dest, "init", "-q")
    git(dest, "symbolic-ref", "HEAD", "refs/heads/main")
    git(dest, "fetch", "-q", f"--depth={int(task.get('history_depth', 200))}", "--no-tags",
        f"file://{mirror_path(repo)}", task["base_commit"])
    git(dest, "checkout", "-q", "-B", "main", "FETCH_HEAD")
    if task.get("overlay"):
        apply_overlay(dest, task["overlay"])
        git(dest, "add", "-A")
        # deterministic and unremarkable: dated like the base commit, neutral message, so the base
        # (and therefore the exported patch's context) is identical for every run and the judge
        when = git(dest, "show", "-s", "--format=%cI", "HEAD").stdout.decode().strip()
        git(dest, "commit", "-q", "-m", task["overlay"].get("message", "Import generated and vendored assets"),
            env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    return dest


def overlay_layers(overlay: dict) -> list[dict]:
    return overlay["layers"] if "layers" in overlay else [overlay]


def apply_overlay(repo_dir: Path, overlay: dict) -> None:
    """Pathological conditions layered on a real repository (committed into the base).

    Layers (`{"layers": [...]}` or a single layer):
      generated_bulk  many generated files; every `mention_every`-th one mentions `mention` (grep noise)
      long_line       one file that is a single line of `bytes` bytes mentioning `mention` (minified asset)
      vendored_copy   copies of real source files under another directory (a decoy definition to edit)
      huge_output     a test module that prints megabytes (content given)
      files           literal files
    """
    for layer in overlay_layers(overlay):
        kind = layer["kind"]
        if kind == "generated_bulk":
            gen = repo_dir / layer.get("dir", "generated")
            mention, every = layer.get("mention"), int(layer.get("mention_every", 0) or 0)
            ext = layer.get("ext", ".py")
            for i in range(int(layer.get("files", 3000))):
                p = gen / f"pkg{i // 100:03d}" / f"gen_{i:05d}{ext}"
                p.parent.mkdir(parents=True, exist_ok=True)
                note = f"# snapshot of {mention} usage, record {i}\n" if mention and every and i % every == 0 else ""
                p.write_text(f"# generated file {i}: do not edit\n{note}VALUE_{i} = {i}\n" + "X = 1\n" * 40)
        elif kind == "long_line":
            p = repo_dir / layer["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            unit = f'{{"k":"{layer.get("mention", "x")}","v":"' + "a" * 200 + '"},'
            n = max(1, int(layer.get("bytes", 2_000_000)) // len(unit))
            p.write_text("[" + unit * n + "{}]")  # no newline anywhere: one enormous line
        elif kind == "vendored_copy":
            for rel in layer["src"]:
                target = repo_dir / layer["dest"] / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repo_dir / rel, target)
        elif kind in ("huge_output",):
            p = repo_dir / layer["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(layer["content"])
        elif kind == "files":
            for rel, content in layer["files"].items():
                p = repo_dir / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
        else:
            raise ValueError(f"unknown overlay {kind}")


# ----------------------------------------------------------------------------- environments
def env_dir(key: str, repo: dict) -> Path:
    return ENVS / key


@contextlib.contextmanager
def env_lock(key: str):
    """Runs of one repository share its toolchain, and an editable install points at one checkout at a
    time. Hold this for a whole run (setup, harness, judge) so parallel runs never test another checkout."""
    ENVS.mkdir(parents=True, exist_ok=True)
    with open(ENVS / f".{key}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def ensure_env(key: str, repo: dict) -> dict[str, str]:
    """Return PATH additions/variables for the repository's toolchain (created once, reused)."""
    env: dict[str, str] = {}
    spec = repo.get("env", {})
    if repo["language"] == "python" and not spec.get("system_python"):
        d = env_dir(key, repo)
        py = d / "bin" / "python"
        stamp = d / ".rehearsal-env.json"
        want = json.dumps({"python": spec.get("python", "python3"), "pip": spec.get("pip", [])}, sort_keys=True)
        if py.exists() and (not stamp.exists() or stamp.read_text() != want):
            shutil.rmtree(d)  # the manifest's toolchain changed: rebuild rather than run against a stale one
        if not py.exists():
            base = shutil.which(spec.get("python", "python3"))
            if base is None:
                raise RuntimeError(f"{key}: interpreter {spec.get('python')} not found")
            subprocess.run([base, "-m", "venv", str(d)], check=True)
            if spec.get("pip"):
                subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                *spec["pip"]], check=True)
            stamp.write_text(want)
        env["PATH_PREPEND"] = str(d / "bin")
        env["VIRTUAL_ENV"] = str(d)
    return env


def prepare_repo(repo_dir: Path, repo: dict, tool_env: dict[str, str], log) -> float:
    """Per-run installation steps inside the task repository (timed; not part of the harness budget)."""
    t0 = time.monotonic()
    for cmd in repo.get("env", {}).get("install", []):
        p = subprocess.run(["bash", "-c", cmd], cwd=repo_dir, env=tool_env, capture_output=True, text=True,
                           timeout=1800)
        if p.returncode != 0:
            raise RuntimeError(f"install step failed: {cmd}: {p.stderr[-600:]}")
    if repo.get("env", {}).get("node_modules_cache"):
        cache = ENVS / f"{repo['mirror']}-node_modules-{git(repo_dir, 'rev-parse', 'HEAD').stdout.decode()[:12]}"
        if not cache.exists():  # without a lockfile, versions resolve at first install and are then cached
            npm = ["npm", "ci"] if (repo_dir / "package-lock.json").exists() else ["npm", "install", "--no-package-lock"]
            subprocess.run([*npm, "--ignore-scripts", "--no-audit", "--no-fund"], cwd=repo_dir, env=tool_env,
                           check=True, capture_output=True, timeout=1800)
            shutil.copytree(repo_dir / "node_modules", cache, symlinks=True)
        elif not (repo_dir / "node_modules").exists():
            subprocess.run(["cp", "-al", str(cache), str(repo_dir / "node_modules")], check=True)
    log(f"  prepared repository in {time.monotonic() - t0:.1f}s")
    return time.monotonic() - t0


def tool_environment(extra: dict[str, str]) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
                     "http_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                     "GOPATH", "GOMODCACHE", "GOFLAGS", "GOPROXY", "CARGO_HOME", "RUSTUP_HOME")}
    env = dict(base)
    if extra.get("PATH_PREPEND"):
        env["PATH"] = extra["PATH_PREPEND"] + os.pathsep + env.get("PATH", "")
    if extra.get("VIRTUAL_ENV"):
        env["VIRTUAL_ENV"] = extra["VIRTUAL_ENV"]
    return env


# ----------------------------------------------------------------------------- per-test verification
def parse_statuses(runner: str, text: str) -> dict[str, str]:
    """Per-test outcome from a runner's verbose output: {test_id: "pass"|"fail"|"skip"}."""
    st: dict[str, str] = {}
    if runner == "pytest":  # run with -rA: "PASSED id", "FAILED id - msg", "ERROR id"
        for status, tid in re.findall(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+?)(?: - .*)?$", text, re.M):
            st[tid] = {"PASSED": "pass", "XFAIL": "pass", "XPASS": "pass", "SKIPPED": "skip"}.get(status, "fail")
    elif runner in ("unittest", "django"):  # verbosity 2: "test_x (mod.Class.test_x) ... ok"
        for name, where, status in re.findall(r"^(\w+) \(([\w.]+)\)(?:\n.*?)?\s\.\.\. (ok|FAIL|ERROR|skipped.*|expected failure"
                                              r"|unexpected success)", text, re.M):
            tid = where if where.endswith("." + name) else f"{where}.{name}"
            st[tid] = "pass" if status in ("ok", "expected failure") else "skip" if status.startswith("skipped") else "fail"
    elif runner == "go":  # go test -v
        for status, tid in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", text, re.M):
            st[tid] = {"PASS": "pass", "SKIP": "skip"}.get(status, "fail")
    elif runner == "mocha":  # --reporter json (output is a JSON document)
        try:
            data = json.loads(text[text.index("{"):])
            for t in data.get("passes", []):
                st[t["fullTitle"]] = "pass"
            for t in data.get("failures", []):
                st[t["fullTitle"]] = "fail"
            for t in data.get("pending", []):
                st[t["fullTitle"]] = "skip"
        except (ValueError, KeyError):
            pass
    return st


def run_verify(repo_dir: Path, verify: dict, tool_env: dict[str, str], timeout: float = 1800) -> dict[str, Any]:
    tests = verify.get("run_tests") or sorted(set(verify["fail_to_pass"]) | set(verify.get("pass_to_pass", [])))
    cmd = verify["cmd"].replace("{tests}", " ".join(tests))
    t0 = time.monotonic()
    try:
        p = subprocess.run(["bash", "-c", cmd], cwd=repo_dir, env=tool_env, capture_output=True, text=True,
                           timeout=timeout)
        out, code = p.stdout + "\n" + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or "") if isinstance(e.stdout, str) else "", None
    st = parse_statuses(verify["runner"], out)
    f2p = {t: st.get(t, "missing") for t in verify["fail_to_pass"]}
    p2p = {t: st.get(t, "missing") for t in verify.get("pass_to_pass", [])}
    return {"f2p": f2p, "p2p": p2p, "exit_code": code, "duration_s": round(time.monotonic() - t0, 2),
            "output_tail": out[-3000:]}


def restore_and_apply_tests(repo_dir: Path, test_patch: str, base_ref: str = "HEAD") -> None:
    """SWE-bench convention: files the hidden test patch touches are reset to the base first, so an
    agent's own edits to those files cannot make the hidden tests unappliable."""
    files = sorted(set(re.findall(r"^diff --git a/(\S+) b/", test_patch, re.M)))
    for f in files:
        if git(repo_dir, "cat-file", "-e", f"{base_ref}:{f}", check=False).returncode == 0:
            git(repo_dir, "checkout", base_ref, "--", f)
        elif (repo_dir / f).exists():
            (repo_dir / f).unlink()
    git(repo_dir, "apply", "--whitespace=nowarn", "-", input=test_patch.encode())


def judge(task: dict, repo: dict, label: dict, patch: bytes, work: Path, log) -> dict[str, Any]:
    """Apply the exported patch to a fresh clean base, add the hidden tests, run the verifier."""
    jr = build_base(task, repo, work / "judge")
    env_extra = ensure_env(task["repo"], repo)
    tenv = tool_environment(env_extra)
    if patch.strip():
        pf = work / "candidate.patch"
        pf.write_bytes(patch)
        chk = git(jr, "apply", "--check", "--whitespace=nowarn", str(pf), check=False)
        if chk.returncode != 0:
            return {"artifact_valid": False, "solved": False,
                    "reason": "exported patch does not apply to a clean base: " + chk.stderr.decode(errors="replace")[:300]}
        git(jr, "apply", "--whitespace=nowarn", str(pf))
    prepare_repo(jr, repo, tenv, log)
    restore_and_apply_tests(jr, label["test_patch"])
    v = run_verify(jr, label["verify"], tenv)
    f2p_ok = all(s == "pass" for s in v["f2p"].values())
    p2p_bad = sorted(t for t, s in v["p2p"].items() if s not in ("pass", "skip"))
    return {"artifact_valid": True, "solved": f2p_ok and not p2p_bad, "f2p": v["f2p"], "p2p_failed": p2p_bad,
            "verify_s": v["duration_s"], "empty_patch": not patch.strip(), "output_tail": v["output_tail"][-1500:]}


# ----------------------------------------------------------------------------- configurations
def compose_profile(base_profile: Path, overrides: dict[str, dict], model: dict | None, dest: Path) -> Path:
    """Write a complete profile: the submission profile + one ablation's policy/tool overrides."""
    from gheerefill.config import load_profile

    prof = load_profile(base_profile, env={})
    d = prof.to_dict()
    for section, vals in overrides.items():
        d[section].update(vals)
    if model:
        d["model"].update(model)
    lines = [f"name = {json.dumps(d['name'] + '+rehearsal')}"]
    for section in ("model", "retry", "limits", "tools", "policy"):
        lines.append(f"\n[{section}]")
        for k, v in d[section].items():
            if v is None or (isinstance(v, (dict, str)) and not v and k in ("extra_body", "extra_headers", "pricing",
                                                                             "script")):
                continue
            lines.append(f"{k} = {toml_value(v)}")
    for rule in d.get("auto") or []:
        lines.append("\n[[auto]]")
        lines += [f"{k} = {toml_value(v)}" for k, v in rule.items()]
    dest.write_text("\n".join(lines) + "\n")
    return dest


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(type(v))


# ----------------------------------------------------------------------------- one judged run
def harness_python() -> str:
    try:
        p = (ROOT / ".harness-python").read_text().strip()
        if p and Path(p).exists():
            return p
    except OSError:
        pass
    return sys.executable


def harness_commit() -> str:
    """The runtime's version: commit, plus "+dirty" when the runtime itself has uncommitted changes."""
    p = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    d = subprocess.run(["git", "status", "--porcelain", "--", "gheerefill", "profiles", "Makefile", "scripts/py.sh"],
                       cwd=ROOT, capture_output=True, text=True)
    return (p.stdout.strip() or "unknown") + ("+dirty" if d.stdout.strip() else "")


def audit(run_dir: Path) -> list[str]:
    """References to the lab (labels, mirrors, task definitions) in what the agent did or saw."""
    markers = [str(REH), "rehearsal/tasks", "rehearsal/repos", "label.b64", "rehearsal/policies"]
    flags = []
    for name in ("transcript.jsonl", "actions.jsonl"):
        for p in run_dir.rglob(name):
            text = p.read_text(errors="replace")
            flags += [f"{p.name}: {m}" for m in markers if m in text]
    return sorted(set(flags))


def final_state_patch(run_dir: Path, base_tree: str, tree: str) -> bytes:
    p = subprocess.run(["git", "--git-dir", str(run_dir / "shadow.git"), "diff", "--binary", "--full-index",
                        "--no-renames", "--no-ext-diff", base_tree, tree], capture_output=True, env=GIT_ENV)
    return p.stdout


def redact_tail(path: Path, secret: str | None, n: int = 1500) -> str:
    try:
        text = path.read_text(errors="replace")[-n:]
    except OSError:
        return ""
    return text.replace(secret, "[REDACTED]") if secret else text


def classify_failure(rec: dict) -> str:
    """Primary failure class of a judged run (precedence order)."""
    if rec.get("solved"):
        return "solved"
    r = rec.get("result") or {}
    if rec.get("harness_timeout"):
        return "harness_timeout"
    if not r:
        return "no_result"
    if r.get("status") == "configuration_error":
        return "configuration_error"
    if r.get("status") != "completed":
        return "infrastructure_error"
    if not rec.get("artifact_valid", True) or not (r.get("deliverable") or {}).get("reconstruction_verified"):
        return "invalid_artifact"
    if rec.get("empty_patch"):
        return "empty_patch"
    term = str(r.get("termination") or "")
    if rec.get("p2p_failed") and all(s == "pass" for s in (rec.get("f2p") or {}).values()):
        return "regression"
    if term.startswith("model_error"):
        return "api_error"
    if term in ("step_limit", "deadline_reached", "token_budget", "cost_budget"):
        return "budget_exhausted_unsolved"
    return "hidden_tests_fail"


def run_one(task: dict, config: str, **kw: Any) -> dict[str, Any]:
    with env_lock(task["repo"]):
        return _run_one(task, config, **kw)


def _run_one(task: dict, config: str, *, repeat: int = 0, policy: str | None = None, faults: list | None = None,
             sig: tuple[str, float] | None = None, key_env: str | None = None, profile: Path | None = None,
             limits_override: dict | None = None, upstream: str | None = None, injection: str | None = None,
             key_mode: str | None = None, emulate: str | None = None, log=print) -> dict[str, Any]:
    manifest = load_json(MANIFEST)
    repo = manifest[task["repo"]]
    configs = load_json(CONFIGS)
    label = read_label(task["dir"])  # read now, but used only after the harness has exited
    rid = f"{task['task_id']}-{config}-r{repeat}-{uuid.uuid4().hex[:6]}"
    work = WORK / rid
    run_out = work / "out"
    log(f"[{rid}] building clean base {task['base_commit'][:12]} of {repo['url']}")
    t_setup = time.monotonic()
    repo_dir = build_base(task, repo, work / "repo")
    env_extra = ensure_env(task["repo"], repo)
    tenv = tool_environment(env_extra)
    prep_s = prepare_repo(repo_dir, repo, tenv, log)
    setup_s = time.monotonic() - t_setup
    limits = {**task.get("limits", {}), **(limits_override or {})}
    model = None
    servers = []
    hen = dict(tenv)
    hen.pop("VIRTUAL_ENV", None)
    if policy:
        from scripts.policy_server import PolicyServer, load_policy  # noqa: PLC0415

        if emulate:  # the policy decides; a DeepSeek-/Qwen-like endpoint shapes the wire behaviour
            from scripts.provider_emulator import ProviderEmulator  # noqa: PLC0415

            fam, _, scale = emulate.partition(":")
            srv = ProviderEmulator(load_policy(policy), fam, seed=repeat, scale=float(scale or 1.0)).__enter__()
        else:
            srv = PolicyServer(load_policy(policy)).__enter__()
        servers.append(srv)
        upstream = srv.base_url.rsplit("/v1", 1)[0]
        model = {"provider": "openai_chat", "name": "scripted-policy", "base_url": srv.base_url,
                 "max_tokens_field": "max_tokens"}
        hen["AI_API_KEY"] = "sk-scripted-policy-not-a-key"
        if key_mode == "missing":
            hen.pop("AI_API_KEY")
    else:
        key_name = key_env or "AI_API_KEY"
        if not os.environ.get(key_name):
            raise SystemExit(f"{key_name} is not set: live rehearsal runs need the prescribed model's key "
                             "(or use --policy for a scripted mechanism rehearsal)")
        hen["AI_API_KEY"] = os.environ[key_name]
    if faults:
        from scripts.fault_proxy import FaultProxy  # noqa: PLC0415

        if upstream is None:
            raise SystemExit("--faults with a live model needs --upstream (the provider root URL)")
        fp = FaultProxy(upstream, faults).__enter__()
        servers.append(fp)
        model = dict(model or {})
        model["base_url"] = fp.base_url + "/v1"
    prof = compose_profile(profile or ROOT / "profiles" / "default.toml", configs[config]["overrides"], model,
                           work / "profile.toml")
    hen.update(ISSUE=task["issue"], REPO=str(repo_dir))
    cmd = [harness_python(), "-m", "gheerefill", "run", "--profile", str(prof), "--out", str(run_out)]
    if limits.get("time_limit_s"):
        cmd += ["--time-limit", str(limits["time_limit_s"])]
    if limits.get("max_steps"):
        cmd += ["--max-steps", str(limits["max_steps"])]
    log(f"[{rid}] launching gheerefill ({config}{', policy ' + policy if policy else ', live model'})")
    t0 = time.monotonic()
    stderr = open(work / "harness.stderr", "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=hen, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=stderr, text=True)
    timeout = float(limits.get("time_limit_s", 1800)) + 240
    harness_timeout = recovered = False
    try:
        if sig:
            time.sleep(sig[1])
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL if sig[0] == "KILL" else signal.SIGTERM)
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        harness_timeout = True
    finally:
        stderr.close()
        emu_stats = next((s.stats() for s in servers if hasattr(s, "stats")), None)
        for s in servers:
            s.__exit__(None, None, None)
    wall = time.monotonic() - t0
    lines = [l for l in (out or "").splitlines() if l.strip().startswith("{")]
    result = json.loads(lines[-1]) if lines else None
    if result is None and sig and sig[0] == "KILL":  # recover offline, exactly as documented
        states = list(run_out.rglob("state.json"))
        if states:
            p = subprocess.run([harness_python(), "-m", "gheerefill", "finalize", "--run-dir", str(states[0].parent)],
                               cwd=ROOT, env=hen, capture_output=True, text=True, timeout=600)
            rl = [l for l in p.stdout.splitlines() if l.strip().startswith("{")]
            result = json.loads(rl[-1]) if rl else None
            recovered = result is not None
    rec: dict[str, Any] = {
        "schema": "gheerefill.rehearsal/v1", "run_id": rid, "task_id": task["task_id"], "repo": task["repo"],
        "repo_url": repo["url"], "base_commit": task["base_commit"], "type": task["type"],
        "size_class": task["size_class"], "config": config, "repeat": repeat,
        "model_kind": "scripted-policy" if policy else "live", "policy": policy, "faults": faults or [],
        "signal": f"{sig[0]}@{sig[1]}" if sig else None, "injection": injection, "key_mode": key_mode,
        "emulate": emulate, "emulator": emu_stats,
        "exit_code": proc.returncode, "stderr_tail": redact_tail(work / "harness.stderr", hen.get("AI_API_KEY")),
        "harness_version": harness_commit(),
        "limits": limits, "setup_s": round(setup_s, 2), "prepare_s": round(prep_s, 2), "wall_s": round(wall, 2),
        "harness_timeout": harness_timeout, "recovered_offline": recovered, "result": None,
    }
    if result:
        run_dir = Path(result.get("run_dir") or "")
        u, t, d = result.get("usage") or {}, result.get("timing") or {}, result.get("deliverable") or {}
        rec.update({
            "result": {k: result.get(k) for k in ("status", "termination", "submission_ready", "task_type",
                                                   "progress", "deliverable")},
            "verification": (result.get("verification") or {}).get("status"),
            "proof_level": (result.get("proof") or {}).get("level"),
            "attempts": len((result.get("proof") or {}).get("attempts") or []),
            "requests": u.get("requests"), "input_tokens": u.get("input_tokens"),
            "output_tokens": u.get("output_tokens"), "tool_calls": sum((u.get("tool_calls") or {}).values()),
            "tool_s": u.get("tool_time_s"), "harness_setup_s": t.get("setup_s"), "solve_s": t.get("solve_s"),
            "total_s": t.get("total_s"), "candidates": result.get("candidates_observed"),
            "candidate_restored": not (result.get("selected_candidate") or {}).get("is_final_state", True),
            "patch_bytes": d.get("patch_bytes"), "harness_reconstruction": d.get("reconstruction_verified"),
            "audit": audit(run_dir) if run_dir.exists() else [],
            "model_quirks": result.get("model_quirks"),
            "advisory_count": max([len(a.get("advisory") or []) for a in (result.get("proof") or {}).get("attempts")
                                   or []] or [0]),
            "run_dir": str(run_dir),
        })
        patch = Path(d["patch_path"]).read_bytes() if d.get("patch_path") and Path(d["patch_path"]).exists() else b""
        log(f"[{rid}] harness: {result.get('status')} / {result.get('termination')} / proof "
            f"{rec['proof_level']}; judging on a fresh clean base")
        j = judge(task, repo, label, patch, work / "judge-selected", log)
        rec.update({k: j.get(k) for k in ("artifact_valid", "solved", "f2p", "p2p_failed", "empty_patch", "reason")})
        final_tree = result.get("final_state_tree")
        sel = (result.get("selected_candidate") or {}).get("tree")
        state = load_json(run_dir / "state.json") if (run_dir / "state.json").exists() else {}
        if final_tree and sel and final_tree != sel and state.get("base_tree"):
            fj = judge(task, repo, label, final_state_patch(run_dir, state["base_tree"], final_tree),
                       work / "judge-final", log)
            rec["final_state_solved"] = fj.get("solved")
            rec["recovery"] = ("win" if rec["solved"] and not fj.get("solved") else
                               "damage" if fj.get("solved") and not rec["solved"] else "neutral")
    rec["failure_class"] = classify_failure(rec)
    log(f"[{rid}] judged: {'SOLVED' if rec.get('solved') else 'not solved'} ({rec['failure_class']})")
    return rec


# ----------------------------------------------------------------------------- gauntlet
GAUNTLET = ROOT / "rehearsal" / "manifests" / "gauntlet.json"


def parse_signal(text: str | None) -> tuple[str, float] | None:
    if not text:
        return None
    kind, _, t = text.partition("@")
    return kind.upper(), float(t)


def gauntlet_plan(manifest: dict, configs: list[str], repeats: int, *, live: bool, upstream: str | None,
                  only: str | None = None) -> tuple[list[dict], list[str]]:
    """Expand the gauntlet manifest into runs: every task clean under every config (live model only),
    then each injection under every config. Returns (runs, notes about what cannot run here)."""
    runs: list[dict] = []
    notes: list[str] = []
    if only in (None, "clean"):
        if live:
            for rep in range(repeats):
                runs += [{"task": t["task"], "config": c, "repeat": rep} for t in manifest["tasks"] for c in configs]
        else:
            notes.append("clean runs skipped: they need the prescribed model (AI_API_KEY)")
    if only in (None, "injections"):
        for inj in manifest["injections"]:
            if inj["mode"] == "live" and not live:
                notes.append(f"injection {inj['id']} skipped: needs the prescribed model")
                continue
            if inj.get("faults") and upstream is None:
                notes.append(f"injection {inj['id']} skipped: fault injection needs --upstream (provider root URL)")
                continue
            runs += [{"task": inj["task"], "config": c, "repeat": 0, "injection": inj} for c in configs]
    return runs, notes


# ----------------------------------------------------------------------------- mechanism rehearsals
MECHANISMS = ROOT / "rehearsal" / "manifests" / "mechanisms.json"


def transcript(run_dir: Path) -> list[dict]:
    p = run_dir / "transcript.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def lab_error_record(task: dict, config: str, e: Exception) -> dict[str, Any]:
    """A run the lab itself could not complete (setup, environment): recorded, never counted as a result."""
    return {"schema": "gheerefill.rehearsal/v1", "task_id": task["task_id"], "repo": task["repo"],
            "base_commit": task["base_commit"], "type": task["type"], "size_class": task["size_class"],
            "config": config, "repeat": 0, "model_kind": "n/a", "harness_version": harness_commit(), "result": None,
            "failure_class": "lab_error", "lab_error": f"{type(e).__name__}: {e}"[:1000]}


def scenario_checks(sc: dict, rec: dict) -> dict[str, Any]:
    """Observable facts about one scripted run, compared against the scenario's expectations."""
    prog = (rec.get("result") or {}).get("progress") or {}
    c: dict[str, Any] = {
        "solved": bool(rec.get("solved")), "artifact_valid": bool(rec.get("artifact_valid")),
        "restored": bool(rec.get("candidate_restored")), "recovered_offline": bool(rec.get("recovered_offline")),
        "interventions": prog.get("interventions", 0), "before_intervention": prog.get("before_intervention", 0),
        "after_intervention": prog.get("after_intervention", 0), "advisory": rec.get("advisory_count") or 0,
        "fails_fast": (not rec.get("solved") and float(rec.get("wall_s") or 1e9) < 90
                       and (rec.get("requests") or 0) <= 1 and (rec.get("exit_code") or 0) != 0
                       and "Traceback" not in (rec.get("stderr_tail") or "")),
    }
    if sc["id"] == "stale_edit" and rec.get("run_dir"):
        tools = [e for e in transcript(Path(rec["run_dir"])) if e.get("role") == "tool"]
        idx = next((i for i, e in enumerate(tools) if "old_str not found" in str(e.get("content"))), None)
        c["stale_edit_refused"] = idx is not None and "it reads exactly" in str(tools[idx].get("content"))
        c["recovered_from_hint"] = (idx is not None and idx + 1 < len(tools)
                                    and not str(tools[idx + 1].get("content")).startswith("Error"))
    return c


def expectation(sc: dict, config: str, checks: dict) -> tuple[bool | None, list[str]]:
    exp = {**sc.get("expect", {}).get("*", {}), **sc.get("expect", {}).get(config, {})}
    if not exp:
        return None, []
    misses = []
    for k, v in exp.items():
        if k.endswith("_min"):
            ok = (checks.get(k[:-4]) or 0) >= v
        elif k.endswith("_max"):
            ok = (checks.get(k[:-4]) or 0) <= v
        else:
            ok = checks.get(k) == v
        if not ok:
            misses.append(f"{k}={v} (observed {checks.get(k[:-4] if k.endswith(('_min', '_max')) else k)})")
    return not misses, misses


def mechanisms(manifest: dict, only: list[str] | None, configs: list[str] | None, out: Path, log=print,
               emulate: str | None = None) -> list[dict]:
    recs = []
    for sc in manifest["scenarios"]:
        if only and sc["id"] not in only:
            continue
        for cfg in configs or sc["configs"]:
            task = load_task(TASKS / sc["task"])
            try:
                rec = run_one(task, cfg, policy=sc["policy"], faults=sc.get("faults"),
                              sig=parse_signal(sc.get("signal")), limits_override=sc.get("limits"),
                              key_mode=sc.get("key_mode"), injection=sc["id"], emulate=emulate or sc.get("emulate"),
                              log=log)
            except Exception as e:  # noqa: BLE001 - one broken run must not end the matrix
                rec = lab_error_record(task, cfg, e)
            rec["scenario"] = sc["id"]
            rec["checks"] = scenario_checks(sc, rec)
            rec["expectation_met"], rec["expectation_misses"] = expectation(sc, cfg, rec["checks"])
            recs.append(rec)
            with open(out / "results.jsonl", "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
            log(f"[{sc['id']}/{cfg}] expectation: {rec['expectation_met']} {rec['expectation_misses']}")
    return recs


def mechanisms_report(manifest: dict, recs: list[dict]) -> str:
    lines = ["# Mechanism rehearsals (scripted policies on real repositories)", "",
             "> " + manifest["about"], "",
             f"Harness {', '.join(sorted({r['harness_version'] for r in recs}))} · {len(recs)} judged runs · "
             f"expectations met: {sum(r['expectation_met'] is True for r in recs)}/"
             f"{sum(r['expectation_met'] is not None for r in recs)}", ""]
    for sc in manifest["scenarios"]:
        rs = [r for r in recs if r.get("scenario") == sc["id"]]
        if not rs:
            continue
        lines += [f"## {sc['id']} — {sc['task']}", "", sc["question"], "",
                  "| config | judged solved | valid artifact | failure class | restored | recovery | interventions "
                  "| fails before / after | advisory | attempts | requests | wall s | expectation |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rs:
            c = r["checks"]
            exp = {True: "met", False: "MISSED: " + "; ".join(r["expectation_misses"]), None: "-"}[r["expectation_met"]]
            lines.append(f"| {r['config']} | {'yes' if c['solved'] else 'no'} | {'yes' if c['artifact_valid'] else 'no'} "
                         f"| {r['failure_class']} | {'yes' if c['restored'] else 'no'} | {r.get('recovery', '-')} | "
                         f"{c['interventions']} | {c['before_intervention']} / {c['after_intervention']} | "
                         f"{c['advisory']} | {r.get('attempts')} | {r.get('requests')} | {r.get('wall_s')} | {exp} |")
        extra = sorted({k for r in rs for k in r["checks"]} - {"solved", "artifact_valid", "restored",
                                                                "recovered_offline", "interventions",
                                                                "before_intervention", "after_intervention",
                                                                "advisory", "fails_fast"})
        if extra or any(r["checks"].get("fails_fast") or r["checks"].get("recovered_offline") for r in rs):
            for r in rs:
                facts = {k: r["checks"][k] for k in extra + ["fails_fast", "recovered_offline"] if k in r["checks"]}
                lines.append(f"\n`{r['config']}`: " + ", ".join(f"{k}={v}" for k, v in facts.items())
                             + (f"; exit code {r.get('exit_code')}" if r.get("result") is None else ""))
        lines.append("")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- validation
def validate(tasks: list[dict], log=print, jobs: int = 1) -> list[dict]:
    """A task is sound when the hidden tests fail on the base, pass with the reference fix, and the
    reference patch applies to a clean base. Tasks of different repositories may run in parallel."""
    if jobs > 1:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        with ThreadPoolExecutor(jobs) as ex:
            rows = list(ex.map(lambda t: validate([t], log)[0], tasks))
        return rows
    manifest = load_json(MANIFEST)
    out = []
    for task in tasks:
        repo = manifest[task["repo"]]
        label = read_label(task["dir"])
        work = WORK / f"validate-{task['task_id']}-{uuid.uuid4().hex[:6]}"
        row: dict[str, Any] = {"task_id": task["task_id"], "ok": False}
        try:
            lock = env_lock(task["repo"])
            lock.__enter__()
            tenv = tool_environment(ensure_env(task["repo"], repo))
            base = build_base(task, repo, work / "base")
            prepare_repo(base, repo, tenv, log)
            restore_and_apply_tests(base, label["test_patch"])
            before = run_verify(base, label["verify"], tenv)
            ref = judge(task, repo, label, label["reference_patch"].encode(), work / "ref", log)
            f2p_base_fail = all(s != "pass" for s in before["f2p"].values())
            p2p_base_ok = all(s in ("pass", "skip") for s in before["p2p"].values())
            row.update(f2p_fail_on_base=f2p_base_fail, p2p_pass_on_base=p2p_base_ok,
                       reference_solves=bool(ref.get("solved")), reference_applies=bool(ref.get("artifact_valid")),
                       f2p=len(label["verify"]["fail_to_pass"]), p2p=len(label["verify"].get("pass_to_pass", [])))
            row["ok"] = f2p_base_fail and p2p_base_ok and row["reference_solves"]
            if not row["ok"]:
                row["detail"] = {"base": before["f2p"], "base_p2p_bad": [t for t, s in before["p2p"].items()
                                                                          if s not in ("pass", "skip")][:10],
                                 "ref": {k: ref.get(k) for k in ("f2p", "p2p_failed", "reason")},
                                 "tail": ref.get("output_tail", "")[-800:]}
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        finally:
            lock.__exit__(None, None, None)
            shutil.rmtree(work, ignore_errors=True)
        log(f"{task['task_id']:<28} {'OK ' if row['ok'] else 'BAD'} "
            + json.dumps({k: row.get(k) for k in ("f2p_fail_on_base", "p2p_pass_on_base", "reference_solves", "f2p",
                                                  "p2p", "error")}))
        out.append(row)
    return out


# ----------------------------------------------------------------------------- localisation recall
def localize_recall(tasks: list[dict], log=print) -> list[dict]:
    """Deterministic: do the harness's localisation hints (no model) point at the files the reference
    fix changed? Measures whether the hints help or mislead; uses labels only after computing hints."""
    from gheerefill import locate

    manifest = load_json(MANIFEST)
    rows = []
    for task in tasks:
        repo = manifest[task["repo"]]
        work = WORK / f"loc-{task['task_id']}"
        base = build_base(task, repo, work)
        files = git(base, "ls-files").stdout.decode().splitlines()
        loc = locate.localize(task["issue"], base, files)
        label = read_label(task["dir"])
        gold = set(label.get("gold_files") or re.findall(r"^diff --git a/(\S+) b/", label["reference_patch"], re.M))
        ranked = [r["path"] for r in loc["ranked"]]
        named = set(loc["files_named"]) | {d["path"] for d in loc["definitions"]}
        related = {f for rel in (loc.get("related") or {}).values() for f in rel.get("imported_by", [])}
        row = {"task_id": task["task_id"], "size_class": task["size_class"], "gold": sorted(gold),
               "hit@1": bool(gold & set(ranked[:1])), "hit@3": bool(gold & set(ranked[:3])),
               "hit@6": bool(gold & set(ranked[:6])), "anchor_hit": bool(gold & named),
               "any_hint_hit": bool(gold & (set(ranked) | named | related)), "elapsed_s": loc["elapsed_s"],
               "files_indexed": loc["files_indexed"], "complete": loc["complete"]}
        rows.append(row)
        shutil.rmtree(work, ignore_errors=True)
        log(f"{task['task_id']:<28} hit@1={row['hit@1']!s:<5} hit@3={row['hit@3']!s:<5} anchors={row['anchor_hit']!s:<5}"
            f" any={row['any_hint_hit']!s:<5} {row['elapsed_s']}s")
    return rows


# ----------------------------------------------------------------------------- reporting
def binom_two_sided(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    probs = [math.comb(n, i) / 2 ** n for i in range(n + 1)]
    return min(1.0, sum(p for p in probs if p <= probs[k] + 1e-12))


def report(records: list[dict]) -> str:
    configs = list(dict.fromkeys(r["config"] for r in records))
    kinds = sorted({r["model_kind"] for r in records})
    lines = ["# Rehearsal report", "",
             f"Runs: {len(records)} · configs: {', '.join(configs)} · model: {', '.join(kinds)} · harness "
             f"{', '.join(sorted({r['harness_version'] for r in records}))}", ""]
    if "scripted-policy" in kinds:
        lines += ["> Scripted-policy runs test the harness's mechanisms under a controlled failure mode; they are "
                  "not evidence about a real model's capability.", ""]
    lines += ["| config | solved | failed | timeout | invalid artifacts | avg requests | avg tool calls | "
              "recovery wins | recovery damage | avg wall s |", "|---|---|---|---|---|---|---|---|---|---|"]
    for c in configs:
        rs = [r for r in records if r["config"] == c]
        solved = sum(bool(r.get("solved")) for r in rs)
        timeouts = sum(r["failure_class"] in ("harness_timeout", "budget_exhausted_unsolved") for r in rs)
        invalid = sum(r["failure_class"] == "invalid_artifact" for r in rs)
        avg = lambda k: sum((r.get(k) or 0) for r in rs) / max(1, len(rs))  # noqa: E731
        lines.append(f"| {c} | {solved}/{len(rs)} | {len(rs) - solved} | {timeouts} | {invalid} | {avg('requests'):.1f} | "
                     f"{avg('tool_calls'):.1f} | {sum(r.get('recovery') == 'win' for r in rs)} | "
                     f"{sum(r.get('recovery') == 'damage' for r in rs)} | {avg('wall_s'):.0f} |")
    lines += ["", "Failure classes:", "", "| config | " + " | ".join(
        sorted({r["failure_class"] for r in records})) + " |",
              "|---|" + "---|" * len({r["failure_class"] for r in records})]
    for c in configs:
        rs = [r for r in records if r["config"] == c]
        lines.append(f"| {c} | " + " | ".join(str(sum(r["failure_class"] == k for r in rs))
                                                for k in sorted({r["failure_class"] for r in records})) + " |")
    if len(configs) > 1:
        a = configs[0]
        lines += ["", f"Paired against `{a}` (same task, same repeat):", ""]
        for b in configs[1:]:
            wins = losses = ties = 0
            for r in records:
                if r["config"] != b:
                    continue
                o = [x for x in records if x["config"] == a and x["task_id"] == r["task_id"] and x["repeat"] == r["repeat"]]
                if o:
                    pa, pb = bool(o[0].get("solved")), bool(r.get("solved"))
                    wins += pb and not pa
                    losses += pa and not pb
                    ties += pa == pb
            lines.append(f"- `{b}` vs `{a}`: wins {wins}, losses {losses}, ties {ties}; sign test "
                         f"p={binom_two_sided(min(wins, losses), wins + losses):.3f}")
    fm = [r for r in records if (r.get("result") or {}).get("progress")]
    if fm:
        lines += ["", "Failure memory (repeated failed repairs, before vs after the first intervention):", "",
                  "| config | runs | interventions | no-progress failures before | after | escalations |",
                  "|---|---|---|---|---|---|"]
        for c in configs:
            rs = [r["result"]["progress"] for r in fm if r["config"] == c]
            if rs:
                lines.append(f"| {c} | {len(rs)} | {sum(p['interventions'] for p in rs)} | "
                             f"{sum(p['before_intervention'] for p in rs)} | {sum(p['after_intervention'] for p in rs)} | "
                             f"{sum(p['escalations'] for p in rs)} |")
    lines += ["", "Per run:", "", "| task | type | size | config | solved | class | proof | attempts | requests | "
              "restored | recovery | audit |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| {r['task_id']} | {r['type']} | {r['size_class']} | {r['config']} | "
                     f"{'yes' if r.get('solved') else 'no'} | {r['failure_class']} | {r.get('proof_level')} | "
                     f"{r.get('attempts')} | {r.get('requests')} | {r.get('candidate_restored')} | "
                     f"{r.get('recovery', '-')} | {len(r.get('audit') or [])} |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--tasks")
    v.add_argument("--jobs", type=int, default=1)
    r = sub.add_parser("run")
    r.add_argument("--task", required=True)
    r.add_argument("--config", default="F")
    r.add_argument("--policy")
    r.add_argument("--faults", help="JSON list for scripts/fault_proxy.py, e.g. '[{\"at\": 2, \"kind\": \"429\"}]'")
    r.add_argument("--signal", help="TERM@SECONDS or KILL@SECONDS (KILL is followed by offline finalize)")
    r.add_argument("--repeat", type=int, default=0)
    r.add_argument("--profile", help="base profile (default: profiles/default.toml)")
    r.add_argument("--out", default=None)
    r.add_argument("--upstream", help="provider root URL the fault proxy forwards to (live runs with --faults)")
    r.add_argument("--emulate", help="with --policy: deepseek|qwen[:scale] provider emulator in front of the policy")
    g = sub.add_parser("gauntlet", help="the Judge Gauntlet (rehearsal/manifests/gauntlet.json)")
    g.add_argument("--configs", default="A,F")
    g.add_argument("--repeats", type=int, default=1)
    g.add_argument("--only", choices=("clean", "injections"))
    g.add_argument("--upstream", help="provider root URL for the tool-failure injection's fault proxy")
    g.add_argument("--manifest", default=str(GAUNTLET))
    g.add_argument("--profile")
    g.add_argument("--out", default=None)
    g.add_argument("--dry-run", action="store_true", help="print the run plan and exit")
    mch = sub.add_parser("mechanisms", help="scripted mechanism rehearsals (rehearsal/manifests/mechanisms.json)")
    mch.add_argument("--scenarios")
    mch.add_argument("--configs")
    mch.add_argument("--out", default=None)
    mch.add_argument("--report", help="only render the report of an existing results.jsonl")
    mch.add_argument("--emulate", help="deepseek|qwen[:scale]: run the scripted policies behind a provider emulator")
    b = sub.add_parser("base", help="build a task's clean base checkout (no labels) at --dest")
    b.add_argument("--task", required=True)
    b.add_argument("--dest", required=True)
    jp = sub.add_parser("judge-patch", help="judge any patch file on a fresh clean base with the hidden tests")
    jp.add_argument("--task", required=True)
    jp.add_argument("--patch", required=True)
    sub.add_parser("localize").add_argument("--tasks")
    rep = sub.add_parser("report")
    rep.add_argument("results")
    args = ap.parse_args()
    names = args.tasks.split(",") if getattr(args, "tasks", None) else None
    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.cmd == "validate":
        rows = validate(all_tasks(names), jobs=args.jobs)
        (RESULTS / "validation.json").write_text(json.dumps(rows, indent=2) + "\n")
        print(f"{sum(r['ok'] for r in rows)}/{len(rows)} tasks valid")
        return 0 if all(r["ok"] for r in rows) else 1
    if args.cmd == "localize":
        rows = localize_recall(all_tasks(names))
        n = max(1, len(rows))
        summary = {k: round(sum(r[k] for r in rows) / n, 3) for k in ("hit@1", "hit@3", "hit@6", "anchor_hit",
                                                                     "any_hint_hit")}
        (RESULTS / "localization.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
        print(json.dumps(summary))
        return 0
    if args.cmd == "report":
        recs = [json.loads(l) for l in Path(args.results).read_text().splitlines() if l.strip()]
        print(report(recs))
        return 0
    if args.cmd == "base":
        task = load_task(TASKS / args.task)
        dest = build_base(task, load_json(MANIFEST)[task["repo"]], Path(args.dest))
        print(dest)
        return 0
    if args.cmd == "judge-patch":
        task = load_task(TASKS / args.task)
        repo = load_json(MANIFEST)[task["repo"]]
        work = WORK / f"judge-{task['task_id']}-{uuid.uuid4().hex[:6]}"
        with env_lock(task["repo"]):
            j = judge(task, repo, read_label(task["dir"]), Path(args.patch).read_bytes(), work,
                      lambda m: print(m, file=sys.stderr))
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({k: j.get(k) for k in ("artifact_valid", "solved", "p2p_failed", "empty_patch", "reason")}))
        return 0 if j.get("solved") else 1
    if args.cmd == "mechanisms":
        manifest = load_json(MECHANISMS)
        if args.report:
            recs = [json.loads(l) for l in Path(args.report).read_text().splitlines() if l.strip()]
        else:
            out = Path(args.out or RESULTS / "runs" / ("mechanisms-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())))
            out.mkdir(parents=True, exist_ok=True)
            recs = mechanisms(manifest, args.scenarios.split(",") if args.scenarios else None,
                              args.configs.split(",") if args.configs else None, out, emulate=args.emulate)
            (out / "report.md").write_text(mechanisms_report(manifest, recs))
        print(mechanisms_report(manifest, recs))
        return 0 if all(r["expectation_met"] is not False for r in recs) else 1
    profile = Path(args.profile) if getattr(args, "profile", None) else None
    if args.cmd == "gauntlet":
        manifest = load_json(Path(args.manifest))
        runs, notes = gauntlet_plan(manifest, [c.strip() for c in args.configs.split(",")], args.repeats,
                                    live=bool(os.environ.get("AI_API_KEY")), upstream=args.upstream, only=args.only)
        for n in notes:
            print("note:", n)
        if args.dry_run or not runs:
            for r_ in runs:
                print(r_["task"], r_["config"], (r_.get("injection") or {}).get("id", "clean"))
            return 0
    out = Path(args.out or RESULTS / "runs" / time.strftime("%Y%m%dT%H%M%S", time.gmtime()))
    out.mkdir(parents=True, exist_ok=True)
    recs = []
    if args.cmd == "run":
        faults = json.loads(args.faults) if args.faults else None
        recs.append(run_one(load_task(Path(args.task)), args.config, repeat=args.repeat, policy=args.policy,
                            faults=faults, sig=parse_signal(args.signal), profile=profile, upstream=args.upstream,
                            emulate=args.emulate))
        with open(out / "results.jsonl", "a") as fh:
            fh.write(json.dumps(recs[-1], default=str) + "\n")
    else:
        for r_ in runs:
            inj = r_.get("injection") or {}
            task = load_task(TASKS / r_["task"])
            try:
                recs.append(run_one(task, r_["config"], repeat=r_["repeat"], policy=inj.get("policy"),
                                    faults=inj.get("faults"), sig=parse_signal(inj.get("signal")),
                                    limits_override=inj.get("limits"), upstream=args.upstream,
                                    injection=inj.get("id"), profile=profile))
            except Exception as e:  # noqa: BLE001
                recs.append(lab_error_record(task, r_["config"], e))
            with open(out / "results.jsonl", "a") as fh:
                fh.write(json.dumps(recs[-1], default=str) + "\n")
    text = report(recs)
    (out / "report.md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
