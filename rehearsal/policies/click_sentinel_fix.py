"""Scripted policy: the historical fix for click_sentinel_copy, verified the way a careful agent would."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import script, tc  # noqa: E402

OLD = '''    def __repr__(self) -> str:
        return f"{self.__class__.__name__}.{self.name}"
'''
NEW = OLD + '''
    def __reduce_ex__(self, protocol: object) -> tuple[t.Any, ...]:
        return getattr, (self.__class__, self.name)
'''
REPRO = "python -c \"import pickle, click; o = click.Option(['--name']); pickle.loads(pickle.dumps(o))\""
respond = script([
    tc("search", pattern="class Sentinel"),
    tc("register_reproduction", command=REPRO, description="pickling a parameter without default"),
    tc("edit_file", path="src/click/_utils.py", old_str=OLD, new_str=NEW),
    tc("bash", command="python -m pytest -q -p no:cacheprovider tests/test_utils/test_sentinel.py tests/test_options.py"),
])
