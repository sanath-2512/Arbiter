TopN output order is unstable for tied counts

`TopN("b a c a b c d", 2)` sometimes returns `[b a]` and sometimes `[c a]` etc. Words with equal
counts must be ordered alphabetically, so the result is deterministic: here `[a b]`.
Counting should also be case-insensitive (`"Go go GO"` counts `go` three times); words are
reported in lower case.
