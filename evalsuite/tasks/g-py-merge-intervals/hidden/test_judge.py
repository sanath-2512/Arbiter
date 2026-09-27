import unittest

from intervals.core import merge


class Judge(unittest.TestCase):
    def test_unsorted(self):
        self.assertEqual(merge([(8, 10), (1, 3), (2, 6)]), [(1, 6), (8, 10)])
