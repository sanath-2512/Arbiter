solve() returns a point where the expression is undefined

```python
>>> from sympy import solve, log, symbols
>>> x = symbols('x')
>>> solve(x/log(x), x)
[0]
>>> solve(x**2/log(x)**2, x)
[0]
```

`x = 0` is not a solution: `log(0)` is not finite, so the expression is undefined there (and `solveset`
correctly returns the empty set). `solve` already drops candidates that make a denominator zero, but not
ones that make it infinite. Results for ordinary cases such as `solve(x/(x - 2), x) == [0]` and
`solve(sin(x)/cos(x), x) == [0, pi]` must not change.
