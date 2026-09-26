"""Load configuration from layered JSON documents."""

import json

from confmerge.merge import deep_merge

DEFAULTS = {"db": {"host": "localhost", "port": 5432}, "features": ["a"], "debug": False}


def load_layers(documents: list[str]) -> dict:
    """Apply JSON documents, in order, on top of DEFAULTS."""
    config = DEFAULTS
    for doc in documents:
        config = deep_merge(config, json.loads(doc))
    return config
