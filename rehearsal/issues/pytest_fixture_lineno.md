--fixtures-per-test reports test locations one line off; fix it and update the tests

```python
import pytest


@pytest.fixture
def arg1():
    """arg1 docstring"""


def test_arg1(arg1):
    pass
```

`pytest --fixtures-per-test test_x.py` prints the section header `(test_x.py:10)` for `test_arg1`, but
`def test_arg1` is on line 9. The helper that formats these `file:line` locations adds 1 to the function's
`co_firstlineno`, which is already 1-based. The same helper is used for the fixture locations in
`--fixtures` / `--fixtures-per-test` (`arg1 -- test_x.py:5`) and in a couple of fixture error messages.

Locations should be exactly the line Python reports for the function (`__code__.co_firstlineno`); for a
decorated fixture that is its first decorator line, and that is acceptable. A number of existing tests
hard-code the shifted line numbers, so update the tests together with the fix.
