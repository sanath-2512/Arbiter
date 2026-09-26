parseDuration ignores everything after the first unit

`parseDuration("1h30m")` returns `3600` instead of `5400`, and `parseDuration("2m5s")` returns
`120` instead of `125`. Compound durations (any combination of `h`, `m`, `s`, in that order) should
be summed. Input that isn't a valid duration (e.g. `""`, `"10"`, `"5x"`, `"1h foo"`) must throw
an `Error` rather than returning a partial result.
