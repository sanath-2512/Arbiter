"""Isolation of model-issued commands from the credential and the host (see sandbox_exec.py).

`Sandbox.probe()` checks what the kernel supports and *verifies* the property that matters: a
sandboxed child cannot read the environment of this process. The verified status is recorded in
every result. When Landlock is unavailable (non-Linux, old kernel, blocked syscall) commands run
unsandboxed and the result says so; nothing is claimed that was not observed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAUNCHER = Path(__file__).with_name("sandbox_exec.py")
CANARY_VAR = "ARBITER_SANDBOX_CANARY"


@dataclass
class Sandbox:
    mode: str = "key"  # off | key | confine
    write_paths: list[str] = field(default_factory=list)
    active: bool = False
    status: dict[str, Any] = field(default_factory=dict)

    def wrap(self, argv: list[str]) -> list[str]:
        if not self.active:
            return list(argv)
        cfg = json.dumps({"mode": self.mode, "write_paths": self.write_paths})
        return [sys.executable, "-I", "-S", str(LAUNCHER), cfg, "--", *argv]

    def probe(self) -> "Sandbox":
        """Decide whether to sandbox, based on an observed self-test (never on assumptions)."""
        if self.mode == "off":
            self.status = {"mode": "off", "active": False, "reason": "disabled by profile"}
            return self
        if not sys.platform.startswith("linux"):
            self.status = {"mode": self.mode, "active": False, "reason": f"Landlock is Linux-only ({sys.platform})"}
            return self
        canary = "canary-" + os.urandom(8).hex()
        cfg = json.dumps({"mode": self.mode, "write_paths": self.write_paths})
        env = {**os.environ, CANARY_VAR: canary}
        # The canary lives in a *child* launched without the sandbox (a stand-in for `make` and the
        # harness): the sandboxed grandchild must not be able to read it.
        probe_parent = (
            "import os, subprocess, sys; "
            f"r = subprocess.run([sys.executable, '-I', '-S', {str(LAUNCHER)!r}, sys.argv[1], '--', 'bash', '-c', "
            "sys.argv[2].replace('PARENT', str(os.getpid()))], capture_output=True, text=True); "
            "print(r.returncode); print(r.stdout.strip()); print(r.stderr.strip()[:300])"
        )
        script = f'tr "\\0" "\\n" < /proc/PARENT/environ 2>/dev/null | grep -c {canary} || true'
        try:
            p = subprocess.run([sys.executable, "-I", "-S", "-c", probe_parent, cfg, script], env=env,
                               capture_output=True, text=True, timeout=20)
            lines = (p.stdout.splitlines() + ["", "", ""])[:3]
            code, count, err = lines[0].strip(), lines[1].strip(), lines[2].strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            self.status = {"mode": self.mode, "active": False, "reason": f"probe failed: {e}"}
            return self
        if code != "0":
            self.status = {"mode": self.mode, "active": False, "reason": err or f"launcher exit {code}"}
            return self
        if count != "0":
            self.status = {"mode": self.mode, "active": False,
                           "reason": "Landlock applied but a sandboxed child could still read its parent's "
                                     "environment (ptrace scoping not effective here)"}
            return self
        self.active = True
        self.status = {"mode": self.mode, "active": True, "mechanism": "landlock domain + no_new_privs + dropped ptrace/admin/perfmon capabilities",
                       "verified": "a sandboxed child could not read a parent process's environment",
                       "write_paths": self.write_paths if self.mode == "confine" else "unrestricted"}
        return self


def confine_paths(repo: Path, scratch: Path) -> list[str]:
    """Writable locations for mode "confine": the task repository, scratch, temp dirs and
    per-user tool caches. Everything else (the harness checkout, run records, system dirs) is
    read-only to model commands."""
    home = Path.home()
    paths = [repo, scratch, Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"), Path("/dev"),
             home / ".cache", home / ".local", home / ".npm", home / ".cargo", home / "go", home / ".m2",
             home / ".gradle", home / ".config", home / ".pyenv", home / ".rustup"]
    import sysconfig

    for key in ("purelib", "platlib", "scripts"):
        try:
            paths.append(Path(sysconfig.get_paths()[key]))
        except KeyError:
            pass
    seen: list[str] = []
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.append(s)
    return seen
