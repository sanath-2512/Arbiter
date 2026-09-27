def to_int(s, default=None):
    try:
        return int(str(s).strip())
    except ValueError:
        return default
