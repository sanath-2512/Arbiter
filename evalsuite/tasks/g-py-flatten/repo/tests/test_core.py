import unittest

from lists.core import flatten


class T(unittest.TestCase):
    def test_one_level(self):
        self.assertEqual(flatten([1, [2]]), [1, 2])
