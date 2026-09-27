"""Scripted policy (interrupted run) for click_sentinel_copy: makes the historical fix, then runs a
long command, so a SIGTERM/SIGKILL sent by the lab lands while a tool subprocess is running with the
fix already on disk. SIGTERM must end in a graceful stop with a valid artifact; after SIGKILL,
`arbiter finalize` must recover one offline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _click_sentinel import FIX_NEW, FIX_OLD, PATH, REPRO, TESTS  # noqa: E402
from _common import script, tc  # noqa: E402

respond = script([
    tc("search", pattern="class Sentinel"),
    tc("register_reproduction", command=REPRO, description="pickling a parameter without a default"),
    tc("edit_file", path=PATH, old_str=FIX_OLD, new_str=FIX_NEW),
    tc("bash", command="sleep 120; " + TESTS, timeout=300),
    tc("bash", command=TESTS),
])
