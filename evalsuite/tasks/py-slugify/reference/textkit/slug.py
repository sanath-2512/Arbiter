"""URL slug helpers."""

import re
import unicodedata

_ALLOWED = re.compile(r"[^a-z0-9]+")


def slugify(text: str, sep: str = "-") -> str:
    """Lower-case `text` and join its ASCII alphanumeric runs with `sep`."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    kept = "".join(ch for ch in decomposed if ch.isascii())
    return _ALLOWED.sub(sep, kept).strip(sep)
