"""Version comparison helpers."""

from distutils.version import LooseVersion


def compare_versions(a: str, b: str) -> int:
    """Return -1 if a < b, 0 if equal, 1 if a > b."""
    va, vb = LooseVersion(a), LooseVersion(b)
    return (va > vb) - (va < vb)


def is_newer(candidate: str, current: str) -> bool:
    return compare_versions(candidate, current) > 0
