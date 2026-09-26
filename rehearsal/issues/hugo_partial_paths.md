Support relative partial paths from within partials

Inside a partial it would help to reference sibling partials relative to the calling partial's own
directory instead of spelling out the full path from `layouts/_partials`:

```
-- layouts/_partials/a/b/main.html --
same:{{ partial "./helper.html" . }}        {{/* layouts/_partials/a/b/helper.html */}}
sub:{{ partial "./sub/helper.html" . }}     {{/* layouts/_partials/a/b/sub/helper.html */}}
parent:{{ partial "../helper.html" . }}     {{/* layouts/_partials/a/helper.html */}}
root:{{ partial "../../root.html" . }}      {{/* layouts/_partials/root.html */}}
```

A partial name starting with `./` or `../` should be resolved relative to the directory of the partial
that makes the call. This should work the same for `partial`, `partialCached` and `partials.Include`,
for names built at run time (`partial (printf "./%s.html" "helper") .`), for partials defined inline with
`{{ define "_partials/a/inline.html" }}` (relative to `_partials/a/`), inside the inner block of a partial
decorator (`{{ with partial "b/wrapper.html" . }}{{ partial "./helper.html" . }}{{ end }}` written in
`_partials/a/main.html` resolves against `_partials/a/`, the partial that contains the block), and
inside `templates.Defer` blocks. `partialCached` must not mix up two different `./cached.html` partials
called from different directories.

Errors (the build should fail with these messages):

- relative path used from a template that is not a partial (e.g. `layouts/home.html`):
  `relative partial path "./helper.html" can only be used from within a partial`
- relative path that resolves outside the partials directory, e.g. `../../helper.html` called from
  `_partials/a/main.html`: `relative partial path "../../helper.html" in "_partials/a/main.html" resolves outside the partials directory`

Partial names without a `./` or `../` prefix keep their current meaning.
