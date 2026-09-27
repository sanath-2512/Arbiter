import unittest

from conv.core import to_int


class T(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(to_int('7'), 7)
