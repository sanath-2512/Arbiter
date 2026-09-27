import unittest

from util.core import chunks


class T(unittest.TestCase):
    def test_even(self):
        self.assertEqual(chunks([1, 2], 1), [[1], [2]])
