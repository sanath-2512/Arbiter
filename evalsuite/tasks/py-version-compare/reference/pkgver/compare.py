"""Version comparison helpers."""

import re

_PRE = {"a": 0, "b": 1, "rc": 2}
_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?$")


def _key(v: str):
    m = _RE.match(v.strip())
    if not m:
        raise ValueError(f"invalid version: {v!r}")
    nums = [int(x) for x in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    pre = (_PRE[m.group(2)], int(m.group(3))) if m.group(2) else (3, 0)
    return nums, pre


def compare_versions(a: str, b: str) -> int:
    """Return -1 if a < b, 0 if equal, 1 if a > b."""
    (na, pa), (nb, pb) = _key(a), _key(b)
    width = max(len(na), len(nb))
    ka = (na + [0] * (width - len(na)), pa)
    kb = (nb + [0] * (width - len(nb)), pb)
    return (ka > kb) - (ka < kb)


def is_newer(candidate: str, current: str) -> bool:
    return compare_versions(candidate, current) > 0
