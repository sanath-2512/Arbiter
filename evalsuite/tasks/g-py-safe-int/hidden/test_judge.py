import unittest

from conv.core import to_int


class Judge(unittest.TestCase):
    def test_lenient(self):
        self.assertEqual(to_int(' 42 '), 42)
        self.assertIsNone(to_int('x'))
        self.assertEqual(to_int('x', 0), 0)
