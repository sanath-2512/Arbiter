import unittest

from intervals.core import merge


class T(unittest.TestCase):
    def test_sorted(self):
        self.assertEqual(merge([(1, 3), (2, 6)]), [(1, 6)])
