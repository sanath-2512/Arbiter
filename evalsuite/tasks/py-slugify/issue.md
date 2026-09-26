slugify() mangles accented text

`slugify("Crème Brûlée")` returns `"crme-brle"`: accented letters are dropped instead of being
transliterated to their ASCII base letter. Expected `"creme-brulee"`.

Also, runs of punctuation/whitespace should collapse into a single separator and the result should
not start or end with a separator: `slugify("  Hello,   World!! ")` should be `"hello-world"`.
The `sep` argument must keep working.
