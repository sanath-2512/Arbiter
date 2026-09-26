`max_warnings` as a TOML integer breaks pytest's native TOML configuration

Using pytest's native TOML configuration (`pytest.toml`, or `[tool.pytest]` in pyproject.toml):

```toml
[pytest]
max_warnings = 1
```

pytest aborts with a TypeError instead of running. Only the quoted form `max_warnings = "1"` works, which is
surprising for a numeric option. Both an integer and the string form should be accepted, and a run whose
warnings exceed the threshold should still end with the max-warnings exit code (an explicit 0 must also
work).
