Rename area_of_circle to circle_area

For consistency with `rectangle_area`, rename `geom.shapes.area_of_circle(r)` to
`circle_area(radius)`. Keep `area_of_circle` working for one release as a deprecated alias that
emits a `DeprecationWarning` and returns the same value. Internal code must use the new name
(nothing in the package should trigger the warning). Both names must stay importable from `geom`.
