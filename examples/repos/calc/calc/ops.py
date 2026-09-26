"""Small arithmetic helpers."""


def add(a, b):
    return a + b


def divide(a, b):
    """Divide a by b."""
    return a // b


def mean(values):
    """Arithmetic mean of a non-empty sequence."""
    return divide(sum(values), len(values))
