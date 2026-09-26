"""URL slug helpers."""

import re

_ALLOWED = re.compile(r"[^a-z0-9]+")


def slugify(text: str, sep: str = "-") -> str:
    """Lower-case `text` and join its ASCII alphanumeric runs with `sep`."""
    lowered = text.lower()
    kept = "".join(ch for ch in lowered if ch.isascii())
    return _ALLOWED.sub(sep, kept)
