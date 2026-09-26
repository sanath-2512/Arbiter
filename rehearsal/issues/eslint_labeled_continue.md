no-unsafe-finally should also report a labeled `continue` that leaves the finally block

`no-unsafe-finally` reports control-flow statements that jump out of a `finally` block (`return`,
`throw`, `break`, and `break label` when the label is outside the block). A labeled `continue` that
targets a loop outside the `finally` block is not reported, although it discards a pending `return` or
exception in exactly the same way:

```js
var foo = function() { a: while (true) try {} finally { while (true) continue a; } }
```

Expected: one `unsafeUsage` error, "Unsafe usage of ContinueStatement.", located on `continue a;`.

These must stay valid: an unlabeled `continue` inside a loop nested in the `finally` block, and a
labeled `continue` whose target loop is itself inside the `finally` block, e.g.
`var foo = function() { try { return 1; } finally { a: while (true) { while (true) continue a; } } }`.
