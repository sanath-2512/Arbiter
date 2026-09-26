"""Scripted policy (wrong generated test): registers a reproduction with a wrong expectation (a pickled
option's missing default comes back as None; the fix correctly keeps the UNSET sentinel), then makes
the correct fix and runs the existing tests. The reproduction fails on the original (for the real
reason, a PicklingError) and keeps failing after the fix (for the wrong expectation). A generated
check is advisory: it must not refute, discard or replace the good candidate."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _click_sentinel import FIX_NEW, FIX_OLD, PATH, TESTS  # noqa: E402
from _common import script, tc  # noqa: E402

WRONG = ("python -c \"import pickle, click; o = click.Option(['--name']); "
         "assert pickle.loads(pickle.dumps(o)).default is None\"")
respond = script([
    tc("search", pattern="class Sentinel"),
    tc("register_reproduction", command=WRONG, description="a pickled option keeps its missing default"),
    tc("edit_file", path=PATH, old_str=FIX_OLD, new_str=FIX_NEW),
    tc("bash", command=TESTS),
])
