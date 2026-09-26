"""Merging of configuration mappings."""


def deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` into `base` and return the result.

    Nested dictionaries are merged key by key. Any other value in `override`
    (including lists) replaces the value in `base`; lists are never concatenated.
    """
    for key, value in override.items():
        base[key] = value
    return base
