import unittest

from geom import area_of_circle, describe, rectangle_area


class GeomTest(unittest.TestCase):
    def test_areas(self):
        self.assertAlmostEqual(area_of_circle(1), 3.14159, places=4)
        self.assertEqual(rectangle_area(2, 3), 6)

    def test_describe(self):
        self.assertEqual(describe("circle", 1), "circle with area 3.14")


if __name__ == "__main__":
    unittest.main()
