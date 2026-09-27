import unittest

from util.core import chunks


class Judge(unittest.TestCase):
    def test_tail(self):
        self.assertEqual(chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
