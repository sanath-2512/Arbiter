import unittest

from calc import add, divide, mean


class Judge(unittest.TestCase):
    def test_true_division(self):
        self.assertEqual(divide(7, 2), 3.5)
        self.assertEqual(divide(-7, 2), -3.5)

    def test_mean(self):
        self.assertEqual(mean([1, 2]), 1.5)
        self.assertEqual(mean([2, 4]), 3)

    def test_unchanged(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(divide(6, 3), 2)
