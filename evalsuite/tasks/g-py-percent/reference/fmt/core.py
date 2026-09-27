def percent(part, total):
    if total == 0:
        return '0.0%'
    return f'{part * 100 / total:.1f}%'
