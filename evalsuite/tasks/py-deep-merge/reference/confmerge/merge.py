"""Merging of configuration mappings."""

import copy


def deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` into `base` and return the result.

    Nested dictionaries are merged key by key. Any other value in `override`
    (including lists) replaces the value in `base`; lists are never concatenated.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
