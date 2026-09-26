"""Human-readable shape summaries."""

from geom.shapes import circle_area, rectangle_area


def describe(shape: str, *dims: float) -> str:
    if shape == "circle":
        return f"circle with area {circle_area(dims[0]):.2f}"
    if shape == "rectangle":
        return f"rectangle with area {rectangle_area(*dims):.2f}"
    raise ValueError(f"unknown shape {shape!r}")
