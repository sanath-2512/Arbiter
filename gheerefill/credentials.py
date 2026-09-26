"""Credential hygiene for the harness process itself.

`take_credential` reads AI_API_KEY once and then removes every copy this process can control:
- the original environment block is overwritten in place, so /proc/<harness pid>/environ no
  longer shows the key (os.environ deletion alone leaves the original bytes readable there);
- the variable is removed from os.environ, so no child inherits it by accident;
- the process is marked non-dumpable, so /proc/<pid>/mem and /proc/<pid>/environ become
  inaccessible to other processes without CAP_SYS_PTRACE.
Ancestors (`make`, the evaluator's shell) still hold the key in their own environments; model
commands are kept away from those by the Landlock sandbox (sandbox.py), not by this module.
"""

from __future__ import annotations

import ctypes
import os
import sys

PR_SET_DUMPABLE = 4
_taken: dict[str, str] = {}


def take_credential(name: str) -> str:
    if name in _taken:
        return _taken[name]
    value = os.environ.get(name, "")
    _taken[name] = value
    if not value:
        return value
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None)
            libc.getenv.restype = ctypes.c_void_p
            ptr = libc.getenv(name.encode())
            if ptr:
                ctypes.memset(ptr, ord("x"), len(value.encode()))
            libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
        except (OSError, AttributeError):
            pass
    os.environ.pop(name, None)
    return value


def taken(name: str) -> str | None:
    return _taken.get(name)
