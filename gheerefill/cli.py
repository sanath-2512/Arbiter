"""Command line entry point (unattended; never prompts, never needs a TTY).

    python -m gheerefill run [--task FILE|-] [--profile FILE] [--out DIR]
    python -m gheerefill finalize --run-dir DIR       # offline recovery from a checkpoint
    python -m gheerefill probe [--profile FILE]       # live endpoint compatibility check
    python -m gheerefill check-config [--profile FILE] [--offline]
    python -m gheerefill verify --run-dir DIR [--rerun]    # offline check of a run's attestation

`run` output protocol (LOCAL DEVELOPMENT PROTOCOL): one JSON result record per task on
stdout, one line each, in input order; human-readable progress on stderr.
Exit status: 0 = every task produced a `completed` record; 1 = at least one invalid input
or infrastructure error; 2 = configuration error (no task attempted).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from gheerefill import __version__
from gheerefill.config import ConfigError, Profile, apply_task_limits, load_profile, profile_from_dict, validate
from gheerefill.records import Redactor, safe_name

HARNESS_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = HARNESS_ROOT / "profiles" / "default.toml"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _emit(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def _caller_dir() -> Path:
    """Directory the user started from (`make` exports it; recipes themselves run in the harness)."""
    return Path(os.environ.get("GHEEREFILL_CALLER_DIR") or os.getcwd())


def _user_path(value: str, *, harness_fallback: bool = False) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    here = _caller_dir() / p
    if harness_fallback and not here.exists() and (HARNESS_ROOT / p).exists():
        return HARNESS_ROOT / p
    return here


def _profile_path(arg: str | None) -> Path:
    value = arg or os.environ.get("GHEEREFILL_PROFILE")
    return _user_path(value, harness_fallback=True) if value else DEFAULT_PROFILE


def _repo_spec(value: str | None) -> str | None:
    if not value or re.match(r"^(https?://|git@|ssh://|file://)", value):
        return value
    return str(_user_path(value))


def _redactor(profile: Profile) -> Redactor:
    return Redactor([os.environ.get(profile.model.api_key_env, ""), os.environ.get("AI_API_KEY", "")])


def _config_error_record(task_id: str | None, message: str) -> dict[str, Any]:
    return {"schema": "gheerefill.result/v1", "task_id": task_id, "status": "configuration_error",
            "submission_ready": False, "error": {"message": message}}


PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
COMMANDS = ("/quit", "/exit", "/help", "quit", "exit", "help")


class PromptReader:
    """Input for the interactive `make run` prompt.

    Pastes are delimited with the terminal's bracketed-paste mode (xterm/VTE, iTerm2, tmux, Windows
    Terminal, VS Code): a multi-line paste ends at the terminal's end marker, so "paste, then Enter"
    submits it even when the text contains blank lines. Typed text, and terminals without the mode,
    end with a line containing only /go (or Ctrl-D). A URL, owner/repo#N or @file line is taken at once.
    Line-mode terminals cap a single line at ~4 KB: very long text is better given as ISSUE=@file.
    """

    def __init__(self, stdin, out):
        from gheerefill.report import is_tty

        self.stdin, self.out = stdin, out
        self.tty = is_tty(stdin) and is_tty(out)

    @contextlib.contextmanager
    def bracketed_paste(self):
        if self.tty:
            self.out.write("\x1b[?2004h")
            self.out.flush()
        try:
            yield
        finally:
            if self.tty:
                self.out.write("\x1b[?2004l")
                self.out.flush()

    def _prompt(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        return self.stdin.readline()

    def read_line(self, prompt: str) -> str:
        return self._prompt(prompt).replace(PASTE_START, "").replace(PASTE_END, "")

    def read_entry(self, prompt: str) -> str | None:
        """One unit of input; None at end of input."""
        from gheerefill.intake import parse_github_ref

        line = self._prompt(prompt)
        if not line:
            return None
        if PASTE_START in line:
            buf = line.split(PASTE_START, 1)[1]
            while PASTE_END not in buf:
                more = self.stdin.readline()
                if not more:
                    break
                buf += more
            text, _, rest = buf.partition(PASTE_END)
            rest = rest.replace(PASTE_START, "").replace(PASTE_END, "").strip()
            return text.replace(PASTE_START, "") + (rest if rest and rest != "/go" else "")
        s = line.strip()
        if not s or s in COMMANDS or s.startswith(("@", "{")) or parse_github_ref(s):
            return line
        self.out.write("(typing: end with a line containing only /go, or Ctrl-D)\n")
        self.out.flush()
        buf = [line]
        while True:
            more = self.stdin.readline()
            if not more or more.strip() == "/go":
                break
            buf.append(more)
        return "".join(buf)


ISSUE_VARS = ("ISSUE", "ISSUE_URL", "GITHUB_ISSUE")
REPO_VARS = ("REPO", "REPO_PATH", "REPO_URL")


def load_dotenv(path: Path) -> list[str]:
    """Developer convenience: fill UNSET variables from a local .env (never committed; values that
    are already in the environment, e.g. the evaluator's AI_API_KEY, always win)."""
    loaded = []
    if not path.is_file():
        return loaded
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v
            loaded.append(k)
    return loaded


def _first_env(names: tuple[str, ...]) -> str | None:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return None


def cmd_run(args: argparse.Namespace) -> int:
    from gheerefill.agent import Agent
    from gheerefill.credentials import take_credential
    from gheerefill.intake import IntakeError, Request, build_task, parse_github_ref, parse_input
    from gheerefill.models import make_client
    from gheerefill.report import card, is_tty, write_report
    from gheerefill.resolve import resolve
    from gheerefill.sandbox import Sandbox

    load_dotenv(HARNESS_ROOT / ".env")
    out_arg = args.out or os.environ.get("GHEEREFILL_OUT")
    out_root = (_user_path(out_arg) if out_arg else HARNESS_ROOT / "runs").resolve()
    workspace = Path(os.environ.get("GHEEREFILL_WORKSPACE") or HARNESS_ROOT / "workspace").resolve()
    human = is_tty(sys.stdout)
    color = human and not os.environ.get("NO_COLOR")
    issue_arg = args.issue or _first_env(ISSUE_VARS)
    if not issue_arg and os.environ.get("ISSUE_FILE"):
        issue_arg = "@" + os.environ["ISSUE_FILE"]
    repo_arg = _repo_spec(args.repo or _first_env(REPO_VARS))
    base_arg = args.base or os.environ.get("BASE") or None
    interactive = not issue_arg and args.task in (None, "-") and is_tty(sys.stdin)
    cli_limits = {k: v for k, v in (("time_limit_s", args.time_limit), ("max_steps", args.max_steps)) if v is not None}

    def emit(record: dict[str, Any]) -> None:
        if not human:
            _emit(record)

    # ---- configuration (fails before any task is attempted)
    config_error = None
    resolution = None
    key = ""
    try:
        profile = load_profile(_profile_path(args.profile))
        validate(profile)
        if profile.model.provider != "fake":
            key = take_credential(profile.model.api_key_env)
            if not key:
                raise ConfigError(f"{profile.model.api_key_env} is not set; export it before `make run`.")
        resolution = resolve(profile, key, discover=not args.no_discover)
        profile.model = resolution.model
        validate(profile)
    except ConfigError as e:
        config_error = str(e)
        profile = None
    gh_token = take_credential("GITHUB_TOKEN") or take_credential("GH_TOKEN") or None
    redactor = Redactor([key, gh_token or ""])
    sandbox_status = Sandbox(profile.policy.sandbox if profile else "key").probe().status

    if human or interactive:
        _err(f"gheerefill {__version__} — autonomous coding harness")
        if resolution:
            _err(f"  model      {resolution.model.provider} · {resolution.model.name} @ {resolution.model.base_url}")
            _err(f"             ({resolution.source}; {resolution.discovery})")
            for n in resolution.notes:
                _err(f"             note: {n}")
        _err("  isolation  " + ("active — " + sandbox_status["verified"] if sandbox_status.get("active")
                                else f"inactive ({sandbox_status.get('reason')})"))
        _err(f"  runs       {out_root}")
    if config_error:
        _err(f"configuration error: {config_error}")
        emit(_config_error_record(None, config_error))
        return 2

    # ---- inputs
    requests: list[Request] = []
    try:
        if issue_arg:
            requests = parse_input(issue_arg, base_dir=_caller_dir(), source="ISSUE")
        elif args.task not in (None, "-"):
            tp = _user_path(args.task)
            if not tp.exists() or tp.is_dir():  # FIFOs, /dev/stdin and process substitution are fine
                _err(f"error: task file not found: {tp}")
                emit(_config_error_record(None, f"task file not found: {tp}"))
                return 2
            with open(tp, encoding="utf-8", errors="replace") as fh:
                requests = parse_input(fh.read(), base_dir=tp.resolve().parent, source=str(tp))
        elif not interactive:
            requests = parse_input(sys.stdin.read(), base_dir=_caller_dir(), source="stdin")
    except IntakeError as e:
        _err(f"invalid input: {e}")
        emit({"schema": "gheerefill.result/v1", "task_id": None, "status": "invalid_input", "submission_ready": False,
              "error": {"message": str(e)}})
        return 1

    exit_code = 0
    current: dict[str, Any] = {}

    def on_signal(signum, frame):  # noqa: ARG001
        agent = current.get("agent")
        if agent is not None:
            _err(f"received signal {signum}; finalising current task")
            agent.request_cancel()
            return
        current["stop"] = True
        if current.get("idle"):
            raise KeyboardInterrupt  # waiting for input: leave now instead of after the next Enter

    def process(req: Request, repo_spec: str | None) -> int:
        if req.error is not None:
            _err(f"invalid task input at {req.error.location}: {req.error.message}")
            emit({"schema": "gheerefill.result/v1", "task_id": req.error.task_id, "status": "invalid_input",
                  "submission_ready": False, "error": {"location": req.error.location, "message": req.error.message}})
            return 1
        try:
            task = build_task(req, repo_spec=repo_spec, base=base_arg, workspace=workspace, github_token=gh_token,
                              limits={}, log=lambda m: _err(redactor.text(m)))
        except IntakeError as e:
            _err(f"cannot prepare task: {e}")
            emit({"schema": "gheerefill.result/v1", "task_id": None, "status": "invalid_input",
                  "submission_ready": False, "error": {"message": str(e), "source": req.source}})
            return 1
        if current.get("stop"):
            emit({"schema": "gheerefill.result/v1", "task_id": task.task_id, "status": "cancelled",
                  "submission_ready": False, "error": {"message": "interrupted before the task started"}})
            return 1
        try:
            tp_profile = apply_task_limits(profile, task.limits)
            if cli_limits:  # TIME_LIMIT= / MAX_STEPS= given to this invocation win over profile and task
                tp_profile = apply_task_limits(tp_profile, cli_limits)
            validate(tp_profile)
        except ConfigError as e:
            emit(_config_error_record(task.task_id, f"invalid task limits: {e}"))
            return 1
        run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        run_dir = out_root / safe_name(task.task_id) / run_id
        repo = task.repo_path.resolve()
        if run_dir == repo or repo in run_dir.parents:
            emit(_config_error_record(task.task_id, f"output directory {run_dir} is inside the target repository"))
            return 1
        client = make_client(tp_profile.model, env={tp_profile.model.api_key_env: key})
        agent = Agent(task, tp_profile, client, run_dir, redactor=redactor, memory_root=out_root / ".memory")
        current["agent"] = agent
        try:
            result = agent.run()
        finally:
            current.pop("agent", None)
        if resolution is not None:
            result["model"]["resolution"] = resolution.to_dict()
        if task.metadata.get("intake_notes"):
            result["intake"] = task.metadata["intake_notes"]
        from gheerefill.records import atomic_write_json

        atomic_write_json(run_dir / "result.json", result)
        try:
            write_report(result, task.issue)
        except Exception as e:  # noqa: BLE001 - the report must never break delivery
            _err(f"report generation failed: {e}")
        if human:
            sys.stdout.write(card(result, color=color))
            sys.stdout.flush()
        else:
            _emit(result)
        if agent.cancel_requested:
            current["stop"] = True
        return 0 if result.get("status") == "completed" else 1

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        if interactive:
            _err("\nSupply the task: a GitHub issue URL (or owner/repo#N), @path/to/issue.md, or paste the issue "
                 "text and press Enter (type /go on its own line if your terminal does not mark pastes). "
                 "Commands: /help, /quit.")
            reader = PromptReader(sys.stdin, sys.stderr)
            with reader.bracketed_paste():
                while not current.get("stop"):
                    try:
                        current["idle"] = True
                        text = reader.read_entry("issue> ")
                        if text is None or text.strip() in ("/quit", "/exit", "quit", "exit"):
                            break
                        if not text.strip():
                            continue
                        if text.strip() in ("/help", "help"):
                            _err("GitHub issue URL | owner/repo#N | @file | pasted text | /quit. Optional: REPO=<path "
                                 "or git URL> for text issues; BASE=<commit|before-issue>; TIME_LIMIT=<s>; MAX_STEPS=<n>.")
                            continue
                        try:
                            reqs = parse_input(text, base_dir=_caller_dir(), source="interactive")
                        except IntakeError as e:
                            _err(f"invalid input: {e}")
                            continue
                        specs = []
                        for req in reqs:
                            spec = repo_arg
                            if req.issue_text is not None and not spec:
                                spec = _repo_spec(reader.read_line("repository (local path or git URL)> ").strip() or None)
                            specs.append(spec)
                    except KeyboardInterrupt:
                        _err("")
                        break
                    finally:
                        current["idle"] = False
                    for req, spec in zip(reqs, specs):
                        exit_code = max(exit_code, process(req, spec))
                        if current.get("stop"):
                            break
        else:
            if not requests:
                _err("no task supplied. Give ISSUE=<GitHub issue URL | owner/repo#N | @file | text> (with REPO=... "
                     "for text), TASK=<file>, pipe the task on stdin, or run `make run` in a terminal.")
                return 0
            for req in requests:
                if current.get("stop"):
                    break
                exit_code = max(exit_code, process(req, repo_arg))
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    return exit_code


def cmd_finalize(args: argparse.Namespace) -> int:
    from gheerefill.agent import Agent
    from gheerefill.task import Task

    run_dir = Path(args.run_dir).resolve()
    try:
        state = json.loads((run_dir / "state.json").read_text())
        task_d = json.loads((run_dir / "task.json").read_text())
        profile = profile_from_dict(json.loads((run_dir / "profile.json").read_text()))
    except (OSError, json.JSONDecodeError, ConfigError) as e:
        _err(f"cannot load checkpoint from {run_dir}: {e}")
        return 2
    if state.get("phase") == "finalized" and not args.force:
        _err("run was already finalised; use --force to finalise again")
        return 2
    try:
        task = Task.from_dict(task_d)
    except (KeyError, TypeError) as e:
        _err(f"invalid task.json in checkpoint: {e}")
        return 2
    if not task.repo_path.is_dir():
        _err(f"repository {task.repo_path} no longer exists; cannot restore into it")
        return 2
    task_sha = hashlib.sha256(json.dumps(task.to_dict(), sort_keys=True).encode()).hexdigest()
    mismatches = []
    if task_sha != state.get("task_sha256"):
        mismatches.append("task")
    if profile.identity() != state.get("profile_id"):
        mismatches.append("profile")
    if str(task.repo_path) != state.get("repo"):
        mismatches.append("repository path")
    if not state.get("base_tree"):
        mismatches.append("base state (never captured)")
    if mismatches:
        _err(f"refusing to resume: checkpoint identity mismatch ({', '.join(mismatches)})")
        return 2
    profile.policy.final_recheck = False
    agent = Agent.recover(run_dir, profile, task)
    result = agent._finalize()
    result["timing"]["total_s"] = round(agent.budget.elapsed(), 3)
    from gheerefill.records import atomic_write_json

    atomic_write_json(run_dir / "result.json", result)
    _emit(result)
    return 0 if result.get("status") == "completed" else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Check a run's attestation offline: patch digest, clean reconstruction, cited evidence."""
    from gheerefill.attest import verify

    report = verify(Path(args.run_dir), rerun=args.rerun)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] and all(r.get("agrees", True) for r in report.get("reruns", [])) else 1


def _resolved_profile(args: argparse.Namespace, *, discover: bool):
    from gheerefill.credentials import take_credential
    from gheerefill.resolve import resolve

    load_dotenv(HARNESS_ROOT / ".env")
    profile = load_profile(_profile_path(args.profile))
    validate(profile)
    key = ""
    if profile.model.provider != "fake":
        key = take_credential(profile.model.api_key_env)
        if not key:
            raise ConfigError(f"{profile.model.api_key_env} is not set")
    resolution = resolve(profile, key, discover=discover)
    profile.model = resolution.model
    validate(profile)
    return profile, key, resolution


def cmd_check_config(args: argparse.Namespace) -> int:
    """Validate the profile, resolve the model and check the key with the provider's model list
    (no tokens are spent). --offline skips the provider call."""
    from gheerefill.sandbox import Sandbox

    try:
        profile, key, resolution = _resolved_profile(args, discover=not args.offline)
    except ConfigError as e:
        _err(f"configuration error: {e}")
        return 2
    print(json.dumps({"ok": True, "resolution": resolution.to_dict(), "profile_id": profile.identity(),
                      "credential_present": bool(key) or profile.model.provider == "fake",
                      "isolation": Sandbox(profile.policy.sandbox).probe().status}, indent=2, default=str))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Live endpoint compatibility: auth, response parsing, native tool call, tool-result turn, usage."""
    from gheerefill.models import make_client
    from gheerefill.models.base import ModelError, ToolSpec

    try:
        profile, key, resolution = _resolved_profile(args, discover=True)
        client = make_client(profile.model, env={profile.model.api_key_env: key})
    except ConfigError as e:
        _err(f"configuration error: {e}")
        return 2
    redactor = Redactor([key])
    tool = ToolSpec("bash", "Run a bash command.", {"type": "object", "properties": {"command": {"type": "string"}},
                                                     "required": ["command"], "additionalProperties": False})
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a connectivity test. Follow instructions exactly."},
        {"role": "user", "content": "Call the bash tool with the command `echo probe-ok`. Do nothing else."},
    ]
    report: dict[str, Any] = {"resolution": resolution.to_dict(), "stream": profile.model.stream,
                              "tool_protocol": profile.model.tool_protocol, "live": profile.model.provider != "fake"}
    try:
        t0 = time.monotonic()
        turn = client.complete(msgs, [tool], timeout_s=profile.model.request_timeout_s)
        report["turn1"] = {"latency_s": round(time.monotonic() - t0, 2), "finish_reason": turn.finish_reason,
                           "tool_calls": [(c.name, c.arguments, c.parse_error) for c in turn.tool_calls],
                           "text": turn.text[:300], "usage": turn.usage.to_dict()}
        report["tool_call_ok"] = bool(turn.tool_calls) and turn.tool_calls[0].name == "bash" and not turn.tool_calls[0].parse_error
        if report["tool_call_ok"]:
            msgs.append(turn.to_message())
            msgs.append({"role": "tool", "tool_call_id": turn.tool_calls[0].id, "name": "bash", "content": "probe-ok"})
            msgs.append({"role": "user", "content": "Reply with the single word DONE."})
            t0 = time.monotonic()
            turn2 = client.complete(msgs, [tool], timeout_s=profile.model.request_timeout_s)
            report["turn2"] = {"latency_s": round(time.monotonic() - t0, 2), "finish_reason": turn2.finish_reason,
                               "text": turn2.text[:300], "tool_calls": len(turn2.tool_calls), "usage": turn2.usage.to_dict()}
            report["tool_result_turn_ok"] = True
        report["usage_reported"] = turn.usage.known
    except ModelError as e:
        report["error"] = {"class": e.cls.value, "status": e.status, "message": e.message[:500]}
    import copy

    alt = copy.deepcopy(profile.model)
    alt.stream = not profile.model.stream
    try:
        t0 = time.monotonic()
        alt_turn = make_client(alt, env={alt.api_key_env: key}).complete(msgs[:2], [tool], timeout_s=profile.model.request_timeout_s)
        report["alternate_mode"] = {"stream": alt.stream, "latency_s": round(time.monotonic() - t0, 2),
                                    "tool_call_ok": bool(alt_turn.tool_calls) and alt_turn.tool_calls[0].name == "bash",
                                    "usage_reported": alt_turn.usage.known}
    except ModelError as e:
        report["alternate_mode"] = {"stream": alt.stream, "error": {"class": e.cls.value, "message": e.message[:300]}}
    print(json.dumps(redactor.obj(report), indent=2, default=str))
    return 0 if report.get("tool_call_ok") and report.get("tool_result_turn_ok") else 1


def _positive(kind):
    def parse(text: str):
        try:
            v = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if v <= 0:
            raise argparse.ArgumentTypeError(f"must be positive: {text!r}")
        return v
    return parse


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # never crash on a non-UTF-8 terminal/locale
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="gheerefill", description=f"gheerefill coding-agent harness {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="solve tasks (JSON / JSON Lines on stdin or --task FILE)")
    p.add_argument("--task", help="task file (JSON object, array, or JSON Lines); '-' or omitted = stdin")
    p.add_argument("--profile", help=f"profile TOML (default: $GHEEREFILL_PROFILE or {DEFAULT_PROFILE})")
    p.add_argument("--out", help="output root for run records (default: $GHEEREFILL_OUT or ./runs)")
    p.add_argument("--issue", help="GitHub issue URL, owner/repo#N, @file or issue text (or ISSUE=...)")
    p.add_argument("--repo", help="repository path or git URL for the issue (or REPO=...)")
    p.add_argument("--base", help="base commit, or 'before-issue' (or BASE=...)")
    p.add_argument("--no-discover", action="store_true", help="skip the provider /models lookup")
    p.add_argument("--time-limit", type=_positive(float), help="wall-clock limit per task in seconds (TIME_LIMIT=...)")
    p.add_argument("--max-steps", type=_positive(int), help="model-turn limit per task (MAX_STEPS=...)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("finalize", help="finalise an interrupted run from its checkpoint (no model calls)")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_finalize)
    p = sub.add_parser("verify", help="check a run's attestation offline (patch digest, reconstruction, evidence)")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--rerun", action="store_true", help="also re-execute each proof check in a temporary copy")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("probe", help="live endpoint compatibility check (uses the credential)")
    p.add_argument("--profile")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("check-config", help="validate profile, resolve the model, check the key (no tokens spent)")
    p.add_argument("--profile")
    p.add_argument("--offline", action="store_true", help="do not contact the provider")
    p.set_defaults(fn=cmd_check_config)
    args = parser.parse_args(argv)
    return args.fn(args)
