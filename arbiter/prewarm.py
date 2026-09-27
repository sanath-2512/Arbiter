"""Background pre-build of a compiled project's tests while the model reads the code.

Rust is the case that matters: a cold `cargo test` compiles every dependency and can take minutes,
which the model would otherwise spend waiting inside its first test command (or lose to a tool
timeout). The harness starts `cargo test --no-run` on the original code at the beginning of the
task. When the registry is unreachable it retries with `--offline` and tells the model to do the
same. The build writes only to `target/` and a new `Cargo.lock`, both outside every snapshot, and
its output is never evidence.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from arbiter.outputs import OutputArchive
from arbiter.shell import read_output_file, run_shell

NETWORK_FAILURE = re.compile(
    r"failed to (download|get|fetch|load source|query replaced source|update)|could not resolve host|"
    r"spurious network error|network failure|unable to update registry|failed to connect|"
    r"Couldn't resolve host|download of config\.json failed|attempting to make an HTTP request", re.I)
FIRST_ERROR = re.compile(r"^(error(\[\w+\])?:.*)$", re.M)


def plan(repo: Path) -> list[str] | None:
    """The pre-build command for this repository, or None when nothing is worth pre-building."""
    if (repo / "Cargo.toml").is_file():
        return ["cargo test --no-run", "cargo test --no-run --offline"]
    return None


class Prewarm:
    def __init__(self, repo: Path, env: dict[str, str], wrap: Callable | None, archive: OutputArchive,
                 timeout_s: float, log: Callable[[str], None]):
        self.repo, self.env, self.wrap, self.archive = repo, env, wrap, archive
        self.timeout_s, self.log = timeout_s, log
        self.commands = plan(repo) or []
        self.cancelled = False
        self.result: dict[str, Any] | None = None
        self._note_taken = False
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self.commands:
            return False
        self._thread = threading.Thread(target=self._run, name="arbiter-prewarm", daemon=True)
        self._thread.start()
        return True

    def _once(self, command: str, timeout_s: float) -> dict[str, Any]:
        oid, out_path = self.archive.allocate()
        r = run_shell(command, cwd=self.repo, env=self.env, timeout_s=timeout_s, output_path=out_path,
                      max_output_bytes=4_000_000, should_cancel=lambda: self.cancelled, wrap=self.wrap)
        self.archive.redact_file(out_path)
        text = read_output_file(out_path, 400_000)
        return {"command": command, "exit_code": r.exit_code, "timed_out": r.timed_out, "cancelled": r.cancelled,
                "duration_s": round(r.duration_s, 1), "output_id": oid, "text": text}

    def _run(self) -> None:
        t0 = time.monotonic()
        try:
            res = self._once(self.commands[0], self.timeout_s)
            if (res["exit_code"] not in (0, None) and len(self.commands) > 1 and not self.cancelled
                    and NETWORK_FAILURE.search(res["text"])):
                left = self.timeout_s - (time.monotonic() - t0)
                if left > 10:
                    offline = self._once(self.commands[1], left)
                    offline["network_failed_first"] = True
                    res = offline
            self.result = res
            self.log(f"pre-build: {res['command']} → exit {res['exit_code']} in {res['duration_s']}s"
                     + (" (timed out)" if res["timed_out"] else ""))
        except Exception as e:  # noqa: BLE001 - an optimisation must never break the run
            self.result = {"error": f"{type(e).__name__}: {e}"}

    def stop(self, wait_s: float = 10.0) -> None:
        self.cancelled = True
        if self._thread is not None:
            self._thread.join(wait_s)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def take_note(self) -> str | None:
        """One message for the model once the pre-build has finished (then None)."""
        if self._note_taken or self.result is None or self.running:
            return None
        self._note_taken = True
        r = self.result
        if "error" in r or r.get("cancelled"):
            return None
        if r["exit_code"] == 0:
            if r.get("network_failed_first"):
                return (f"[harness] Crates cannot be downloaded here, but `{r['command']}` built the tests from the "
                        f"local cache in {r['duration_s']:.0f}s: add --offline to your cargo commands.")
            return (f"[harness] The tests were pre-built in the background (`{r['command']}`, {r['duration_s']:.0f}s); "
                    "cargo runs now rebuild only what you change.")
        if r["timed_out"]:
            return (f"[harness] Pre-building the tests (`{r['command']}`) did not finish in {r['duration_s']:.0f}s: "
                    "this crate builds slowly; give cargo commands a long timeout and test narrowly (-p <crate>, "
                    "a test-name filter).")
        err = FIRST_ERROR.search(r["text"])
        why = err.group(1)[:200] if err else f"exit code {r['exit_code']}"
        if NETWORK_FAILURE.search(r["text"]):
            return (f"[harness] The crate's dependencies are not available here (download failed and the local cache "
                    f"lacks them): {why}. Test what builds without them.")
        return (f"[harness] Building the tests on the original code failed ({r['command']}): {why}. The failure is "
                f"there before any change; full output: read_output(id=\"{r['output_id']}\").")

    def summary(self) -> dict[str, Any] | None:
        if self.result is None:
            return {"command": self.commands[0], "state": "running" if self.running else "not finished"} \
                if self.commands else None
        return {k: v for k, v in self.result.items() if k != "text"}
