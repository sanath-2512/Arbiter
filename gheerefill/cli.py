"""Command line entry point (unattended; never prompts, never needs a TTY).

    python -m gheerefill run [--task FILE|-] [--profile FILE] [--out DIR]
    python -m gheerefill finalize --run-dir DIR       # offline recovery from a checkpoint
    python -m gheerefill probe [--profile FILE]       # live endpoint compatibility check
    python -m gheerefill check-config [--profile FILE]

`run` output protocol (LOCAL DEVELOPMENT PROTOCOL): one JSON result record per task on
stdout, one line each, in input order; human-readable progress on stderr.
Exit status: 0 = every task produced a `completed` record; 1 = at least one invalid input
or infrastructure error; 2 = configuration error (no task attempted).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def _profile_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("GHEEREFILL_PROFILE") or DEFAULT_PROFILE)


def _redactor(profile: Profile) -> Redactor:
    return Redactor([os.environ.get(profile.model.api_key_env, ""), os.environ.get("AI_API_KEY", "")])


def _config_error_record(task_id: str | None, message: str) -> dict[str, Any]:
    return {"schema": "gheerefill.result/v1", "task_id": task_id, "status": "configuration_error",
            "submission_ready": False, "error": {"message": message}}


def cmd_run(args: argparse.Namespace) -> int:
    from gheerefill.agent import Agent
    from gheerefill.models import make_client
    from gheerefill.task import Task, TaskInputError, iter_tasks

    out_root = Path(args.out or os.environ.get("GHEEREFILL_OUT") or HARNESS_ROOT / "runs").resolve()
    config_error = None
    try:
        profile = load_profile(_profile_path(args.profile))
        validate(profile)
        if profile.model.provider != "fake":
            from gheerefill.models import read_api_key

            read_api_key(profile.model)
    except ConfigError as e:
        config_error = str(e)
        profile = None
    if args.task in (None, "-"):
        stream, base_dir, source = sys.stdin, Path.cwd(), "stdin"
    else:
        tp = Path(args.task)
        if not tp.is_file():
            _err(f"error: task file not found: {tp}")
            _emit(_config_error_record(None, f"task file not found: {tp}"))
            return 2
        stream, base_dir, source = open(tp, encoding="utf-8"), tp.resolve().parent, str(tp)
    exit_code = 0
    current: dict[str, Any] = {}

    def on_signal(signum, frame):  # noqa: ARG001
        _err(f"received signal {signum}; finalising current task")
        agent = current.get("agent")
        if agent is not None:
            agent.request_cancel()
        else:
            current["stop"] = True

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        for item in iter_tasks(stream, base_dir=base_dir, source=source):
            if current.get("stop"):
                break
            if isinstance(item, TaskInputError):
                _err(f"invalid task input at {item.location}: {item.message}")
                _emit({"schema": "gheerefill.result/v1", "task_id": item.task_id, "status": "invalid_input",
                       "submission_ready": False, "error": {"location": item.location, "message": item.message}})
                exit_code = max(exit_code, 1)
                continue
            task: Task = item
            if config_error or profile is None:
                _err(f"configuration error: {config_error}")
                _emit(_config_error_record(task.task_id, config_error or "invalid profile"))
                exit_code = 2
                continue
            try:
                tp_profile = apply_task_limits(profile, task.limits)
                validate(tp_profile)
            except ConfigError as e:
                _emit(_config_error_record(task.task_id, f"invalid task limits: {e}"))
                exit_code = max(exit_code, 1)
                continue
            run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
            run_dir = out_root / safe_name(task.task_id) / run_id
            repo = task.repo_path.resolve()
            if run_dir == repo or repo in run_dir.parents:
                _emit(_config_error_record(task.task_id, f"output directory {run_dir} is inside the target repository"))
                exit_code = max(exit_code, 1)
                continue
            redactor = _redactor(tp_profile)
            client = make_client(tp_profile.model)
            agent = Agent(task, tp_profile, client, run_dir, redactor=redactor)
            current["agent"] = agent
            try:
                result = agent.run()
            finally:
                current.pop("agent", None)
            _emit(result)
            if result.get("status") != "completed":
                exit_code = max(exit_code, 1)
            if agent.cancel_requested:
                current["stop"] = True
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if stream is not sys.stdin:
            stream.close()
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


def cmd_check_config(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(_profile_path(args.profile))
        validate(profile)
        key_ok = True
        if profile.model.provider != "fake":
            from gheerefill.models import read_api_key

            read_api_key(profile.model)
    except ConfigError as e:
        _err(f"configuration error: {e}")
        return 2
    d = profile.to_dict()
    print(json.dumps({"ok": True, "profile": d, "profile_id": profile.identity(), "credential_present": key_ok},
                     indent=2, default=str))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Live endpoint compatibility: auth, response parsing, native tool call, tool-result turn, usage."""
    from gheerefill.models import make_client
    from gheerefill.models.base import ModelError, ToolSpec

    try:
        profile = load_profile(_profile_path(args.profile))
        validate(profile)
        client = make_client(profile.model)
    except ConfigError as e:
        _err(f"configuration error: {e}")
        return 2
    redactor = _redactor(profile)
    tool = ToolSpec("bash", "Run a bash command.", {"type": "object", "properties": {"command": {"type": "string"}},
                                                     "required": ["command"], "additionalProperties": False})
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a connectivity test. Follow instructions exactly."},
        {"role": "user", "content": "Call the bash tool with the command `echo probe-ok`. Do nothing else."},
    ]
    report: dict[str, Any] = {"provider": profile.model.provider, "model": profile.model.name,
                              "base_url": profile.model.base_url, "stream": profile.model.stream,
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
    # The other transport mode, one request only, so the profile's `stream` choice is evidence-based.
    import copy

    alt = copy.deepcopy(profile.model)
    alt.stream = not profile.model.stream
    try:
        t0 = time.monotonic()
        alt_turn = make_client(alt).complete(msgs[:2], [tool], timeout_s=profile.model.request_timeout_s)
        report["alternate_mode"] = {"stream": alt.stream, "latency_s": round(time.monotonic() - t0, 2),
                                    "tool_call_ok": bool(alt_turn.tool_calls) and alt_turn.tool_calls[0].name == "bash",
                                    "usage_reported": alt_turn.usage.known}
    except ModelError as e:
        report["alternate_mode"] = {"stream": alt.stream, "error": {"class": e.cls.value, "message": e.message[:300]}}
    print(json.dumps(redactor.obj(report), indent=2, default=str))
    return 0 if report.get("tool_call_ok") and report.get("tool_result_turn_ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gheerefill", description=f"gheerefill coding-agent harness {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="solve tasks (JSON / JSON Lines on stdin or --task FILE)")
    p.add_argument("--task", help="task file (JSON object, array, or JSON Lines); '-' or omitted = stdin")
    p.add_argument("--profile", help=f"profile TOML (default: $GHEEREFILL_PROFILE or {DEFAULT_PROFILE})")
    p.add_argument("--out", help="output root for run records (default: $GHEEREFILL_OUT or ./runs)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("finalize", help="finalise an interrupted run from its checkpoint (no model calls)")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_finalize)
    p = sub.add_parser("probe", help="live endpoint compatibility check (uses the credential)")
    p.add_argument("--profile")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("check-config", help="validate profile and credential presence (no network)")
    p.add_argument("--profile")
    p.set_defaults(fn=cmd_check_config)
    args = parser.parse_args(argv)
    return args.fn(args)
