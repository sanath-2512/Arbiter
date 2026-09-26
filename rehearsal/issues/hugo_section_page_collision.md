Regular page next to a section of the same name is replaced by a section (regression since v0.152.2)

```
-- hugo.toml --
disableKinds = ['home','rss','sitemap','taxonomy','term']
-- content/s1.md --
---
title: content/s1.md
---
-- content/s1/p1.md --
---
title: content/s1/p1.md
---
-- layouts/all.html --
{{ .Title }}
```

With v0.152.2 `public/s1/index.html` rendered the page `content/s1.md`. With the current version the
page is overwritten by an automatically created section for `s1`, so its content is lost. The same
happens with `uglyURLs = true`, where `public/s1.html` should contain `content/s1.md`. The section's own
page (`public/s1/p1/index.html`, or `public/s1/p1.html` with ugly URLs) must keep rendering
`content/s1/p1.md`.
