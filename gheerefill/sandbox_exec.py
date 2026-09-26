"""Standalone launcher: enter a Landlock domain, then exec the command.

Run as `python -I -S sandbox_exec.py '<json config>' -- argv...` (no package imports, so the
per-command overhead is ~20 ms). Linux only; unprivileged (no root, no namespaces, no packages).

Effects, in order:
1. Drop CAP_SYS_PTRACE, CAP_SYS_ADMIN, CAP_PERFMON (+ BPF, CHECKPOINT_RESTORE) from the bounding
   set (only possible as root; harmless otherwise). Each of the first three independently lets
   root bypass the ptrace scoping below; they are lost at the exec of the command.
2. no_new_privs (required by Landlock; setuid binaries such as sudo will not gain privileges).
3. Landlock ruleset handling write-type rights. mode "key" grants them on "/" (no functional
   file-system restriction); mode "confine" grants them only beneath the configured paths.
   Either way the process now lives in a Landlock domain, and the kernel forbids it from
   ptrace-accessing processes outside the domain — which includes reading their
   /proc/<pid>/environ and /proc/<pid>/mem. The harness, `make` and the evaluator's shell hold
   AI_API_KEY in their environments; commands in the domain cannot read it.

Any failure is fatal (exit 126) so a command never runs unsandboxed while the harness reports
that it is sandboxed. Availability is probed by the parent before this launcher is used.
"""

import ctypes
import json
import os
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446  # same on all Linux arches
PR_SET_NO_NEW_PRIVS, PR_CAPBSET_DROP = 38, 24
# Capabilities that each (independently, verified empirically) let a root process read another
# process's /proc/<pid>/environ despite Landlock scoping: SYS_PTRACE (19), SYS_ADMIN (21),
# PERFMON (38). BPF (39) and CHECKPOINT_RESTORE (40) are other process-introspection paths.
DROP_CAPS = (19, 21, 38, 39, 40)
RULE_PATH_BENEATH = 1

# LANDLOCK_ACCESS_FS_* bits
WRITE_FILE, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 4, 1 << 5
MAKE_CHAR, MAKE_DIR, MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = (1 << i for i in range(6, 13))
REFER, TRUNCATE = 1 << 13, 1 << 14


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def fail(msg: str) -> None:
    sys.stderr.write(f"[sandbox] refusing to run unsandboxed: {msg}\n")
    sys.stderr.flush()
    os._exit(126)


def main() -> None:
    cfg = json.loads(sys.argv[1])
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        fail("usage: sandbox_exec.py CONFIG -- ARGV...")
    argv = sys.argv[3:]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    for cap in DROP_CAPS:
        libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)  # EPERM without CAP_SETPCAP (not root): nothing to drop
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        fail(f"no_new_privs failed (errno {ctypes.get_errno()})")
    abi = libc.syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if abi < 1:
        fail(f"Landlock unavailable (errno {ctypes.get_errno()})")
    handled = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | \
        MAKE_BLOCK | MAKE_SYM
    if abi >= 2:
        handled |= REFER
    if abi >= 3:
        handled |= TRUNCATE
    attr = RulesetAttr(handled)
    rfd = libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if rfd < 0:
        fail(f"create_ruleset failed (errno {ctypes.get_errno()})")
    paths = ["/"] if cfg.get("mode", "key") == "key" else list(cfg.get("write_paths", []))
    for p in paths:
        try:
            fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            continue  # a configured path that does not exist grants nothing
        rule = PathBeneathAttr(handled, fd)
        if libc.syscall(SYS_ADD_RULE, ctypes.c_int(rfd), ctypes.c_int(RULE_PATH_BENEATH), ctypes.byref(rule),
                        ctypes.c_uint32(0)) != 0:
            fail(f"add_rule {p} failed (errno {ctypes.get_errno()})")
        os.close(fd)
    if libc.syscall(SYS_RESTRICT_SELF, ctypes.c_int(rfd), ctypes.c_uint32(0)) != 0:
        fail(f"restrict_self failed (errno {ctypes.get_errno()})")
    os.close(rfd)
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        sys.stderr.write(f"{argv[0]}: {e}\n")
        os._exit(127)


if __name__ == "__main__":
    main()
