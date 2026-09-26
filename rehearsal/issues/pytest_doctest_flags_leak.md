Inline doctest directives leak into the next docstring after a failure, skip or xfail

With `--doctest-modules`, a `# doctest: +FLAG` / `-FLAG` directive should only apply to its own example.
When that example fails, errors, or calls `pytest.skip()` / `pytest.xfail()`, the flag stays active for the
docstrings that follow in the same module:

```python
def first():
    '''
    >>> 1 / 0  # doctest: +ELLIPSIS
    2.
    '''

def second():
    '''
    >>> print("foobar")
    foo...
    '''
```

With no `doctest_optionflags` configured, `second` should fail (ELLIPSIS is not enabled for it) but it
passes. The opposite happens with `doctest_optionflags = ELLIPSIS` and a `-ELLIPSIS` directive in `first`:
`second` then fails although it should pass. Happens with and without `--doctest-continue-on-failure`.
