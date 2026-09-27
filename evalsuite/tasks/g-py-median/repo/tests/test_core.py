import unittest

from stats.core import median


class T(unittest.TestCase):
    def test_odd(self):
        self.assertEqual(median([1, 2, 3]), 2)
