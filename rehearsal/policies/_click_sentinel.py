"""Shared pieces of the click_sentinel_copy scripted policies (the historical fix, written by hand)."""

PATH = "src/click/_utils.py"
FIX_OLD = '''    def __repr__(self) -> str:
        return f"{self.__class__.__name__}.{self.name}"
'''
FIX_NEW = FIX_OLD + '''
    def __reduce_ex__(self, protocol: object) -> tuple[t.Any, ...]:
        return getattr, (self.__class__, self.name)
'''
REPRO = "python -c \"import pickle, click; o = click.Option(['--name']); pickle.loads(pickle.dumps(o))\""
TESTS = "python -m pytest -q -p no:cacheprovider tests/test_utils/test_sentinel.py tests/test_options.py"
