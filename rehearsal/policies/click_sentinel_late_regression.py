"""Scripted policy (bad candidate): the historical fix is made and verified (candidate A), then an
unverified "tidy-up" of Sentinel.__repr__ (candidate B) is submitted. B breaks an existing test that
passes on the original code. A harness that ships its final state exports B; candidate preservation
must export A. Measures harness plumbing, not model capability."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _click_sentinel import FIX_NEW, FIX_OLD, PATH, REPRO, TESTS  # noqa: E402
from _common import script, tc  # noqa: E402

respond = script([
    tc("search", pattern="class Sentinel"),
    tc("register_reproduction", command=REPRO, description="pickling a parameter without a default"),
    tc("edit_file", path=PATH, old_str=FIX_OLD, new_str=FIX_NEW),
    tc("bash", command=TESTS),
    tc("edit_file", path=PATH, old_str='        return f"{self.__class__.__name__}.{self.name}"\n',
       new_str='        return f"<{self.name}>"\n'),
])
