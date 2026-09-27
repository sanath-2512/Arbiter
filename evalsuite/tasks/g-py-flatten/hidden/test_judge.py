import unittest

from lists.core import flatten


class Judge(unittest.TestCase):
    def test_deep(self):
        self.assertEqual(flatten([1, [2, [3, [4]]]]), [1, 2, 3, 4])
