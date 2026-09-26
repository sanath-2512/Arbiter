"""Scripted policy (hostile tools) for click_sentinel_copy: before making the historical fix it runs a
command that hangs (tool timeout, process group must be killed), one that prints tens of megabytes
(bounded capture, retrieval hints), an ambiguous edit (refused: old_str matches several places) and an
edit of a file that does not exist; then the fix and the tests. The run must survive all of it and
export the fix."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _click_sentinel import FIX_NEW, FIX_OLD, PATH, REPRO, TESTS  # noqa: E402
from _common import script, tc  # noqa: E402

respond = script([
    tc("bash", command="sleep 600; echo never", timeout=5),
    tc("bash", command="python -c \"import sys; sys.stdout.write('x' * 40_000_000)\""),
    tc("bash", command="(sleep 900 &) ; echo started a background sleeper"),
    tc("edit_file", path=PATH, old_str="object()", new_str="object()  # sentinel"),
    tc("edit_file", path="src/click/does_not_exist.py", old_str="a", new_str="b"),
    tc("register_reproduction", command=REPRO, description="pickling a parameter without a default"),
    tc("edit_file", path=PATH, old_str=FIX_OLD, new_str=FIX_NEW),
    tc("bash", command=TESTS),
])
