import unittest

from stats.core import median


class Judge(unittest.TestCase):
    def test_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)
        self.assertEqual(median([3, 1, 2]), 2)
