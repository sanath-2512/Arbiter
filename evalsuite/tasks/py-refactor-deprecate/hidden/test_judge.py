import math
import unittest
import warnings

import geom
from geom import circle_area, describe, rectangle_area
from geom.shapes import area_of_circle


class Judge(unittest.TestCase):
    def test_new_name(self):
        self.assertAlmostEqual(circle_area(2), math.pi * 4)
        self.assertAlmostEqual(circle_area(radius=1), math.pi)

    def test_alias_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self.assertAlmostEqual(geom.area_of_circle(1), math.pi)
            self.assertAlmostEqual(area_of_circle(1), math.pi)
        self.assertTrue(w and all(issubclass(x.category, DeprecationWarning) for x in w))

    def test_internal_code_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(describe("circle", 1), "circle with area 3.14")
            self.assertEqual(describe("rectangle", 2, 3), "rectangle with area 6.00")
        self.assertEqual(rectangle_area(2, 3), 6)
