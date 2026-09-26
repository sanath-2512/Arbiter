"""Bounded subprocess execution with process-group cleanup.

Adapted in spirit from mini-swe-agent's LocalEnvironment (`_run` kills the whole process
group on timeout; MIT License, (c) 2025 Kilian A. Lieret and Carlos E. Jimenez).
Differences: output is pumped from a pipe into a file (no unbounded memory) with an exact size cap, an
external cancel check, SIGTERM->SIGKILL escalation, and cleanup of leftover background
processes in the group after the main process exits. Processes that call setsid()
escape the group; this is not a sandbox.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SECRET_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_ACCESS_KEY", "_PASSWORD", "_CREDENTIALS")
SECRET_NAMES = {"AI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}


def tool_environment(base: dict[str, str], scratch: Path, extra_secret_names: tuple[str, ...] = ()) -> dict[str, str]:
    """Environment for model-issued commands: credentials removed, non-interactive defaults.
    Filtering variables is NOT process isolation (see NOTES.md)."""
    drop = SECRET_NAMES | set(extra_secret_names)
    env = {
        k: v
        for k, v in base.items()
        if k not in drop and not k.upper().endswith(SECRET_NAME_SUFFIXES) and not k.startswith("GHEEREFILL_")
    }
    env.update(
        {
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "MANPAGER": "cat",
            "LESS": "-R",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PIP_PROGRESS_BAR": "off",
            "PIP_NO_INPUT": "1",
            "TQDM_DISABLE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "DEBIAN_FRONTEND": "noninteractive",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "HARNESS_SCRATCH": str(scratch),
        }
    )
    return env


@dataclass
class ShellResult:
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    output_limit_hit: bool
    duration_s: float
    output_path: Path
    output_bytes: int
    leftover_processes_killed: bool


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_group(pgid: int, grace_s: float = 1.0) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.monotonic() + grace_s
    while time.monotonic() < end and _group_alive(pgid):
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_shell(
    command: str | list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    output_path: Path,
    max_output_bytes: int,
    should_cancel: Callable[[], bool] | None = None,
) -> ShellResult:
    """Run a command in its own process group; stream merged stdout/stderr through a pipe into
    `output_path`, stopping (and killing the group) once `max_output_bytes` is reached."""
    argv = ["bash", "-c", command] if isinstance(command, str) else list(command)
    t0 = time.monotonic()
    timed_out = cancelled = False
    state = {"bytes": 0, "limit_hit": False}
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pgid = proc.pid

    def pump() -> None:
        assert proc.stdout is not None
        with open(output_path, "wb") as out:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    return
                room = max_output_bytes - state["bytes"]
                if room <= 0:
                    state["limit_hit"] = True
                    return
                out.write(chunk[:room])
                state["bytes"] += min(len(chunk), room)
                if len(chunk) > room:
                    state["limit_hit"] = True
                    return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = t0 + max(0.1, timeout_s)
    while True:
        try:
            proc.wait(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            timed_out = True
        elif should_cancel is not None and should_cancel():
            cancelled = True
        if timed_out or cancelled or state["limit_hit"]:
            _kill_group(pgid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            break
    leftovers = _group_alive(pgid)
    if leftovers:
        _kill_group(pgid, grace_s=0.3)
    reader.join(timeout=3)
    try:
        if proc.stdout is not None and not reader.is_alive():
            proc.stdout.close()
    except OSError:
        pass
    limit_hit = state["limit_hit"]
    size = os.path.getsize(output_path) if output_path.exists() else 0
    return ShellResult(
        exit_code=proc.returncode if not (timed_out or cancelled or limit_hit) else None,
        timed_out=timed_out,
        cancelled=cancelled,
        output_limit_hit=limit_hit,
        duration_s=time.monotonic() - t0,
        output_path=output_path,
        output_bytes=size,
        leftover_processes_killed=leftovers,
    )


def read_output_file(path: Path, max_bytes: int = 8 * 1024 * 1024) -> str:
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_bytes().decode("utf-8", errors="replace")
    with open(path, "rb") as fh:
        head = fh.read(max_bytes // 4)
        fh.seek(size - (max_bytes * 3 // 4))
        tail = fh.read()
    return (
        head.decode("utf-8", errors="replace")
        + f"\n[... {size - len(head) - len(tail)} bytes of very large output not loaded; see archive file ...]\n"
        + tail.decode("utf-8", errors="replace")
    )
