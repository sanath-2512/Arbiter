"""Area helpers."""

import math
import warnings


def circle_area(radius: float) -> float:
    return math.pi * radius * radius


def area_of_circle(r: float) -> float:
    """Deprecated alias of circle_area."""
    warnings.warn("area_of_circle is deprecated; use circle_area", DeprecationWarning, stacklevel=2)
    return circle_area(r)


def rectangle_area(width: float, height: float) -> float:
    return width * height
