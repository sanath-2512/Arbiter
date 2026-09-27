def median(values):
    v = sorted(values)
    n = len(v)
    if n % 2 == 0:
        return (v[n // 2 - 1] + v[n // 2]) / 2
    return v[n // 2]
