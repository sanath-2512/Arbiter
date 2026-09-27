import unittest

from fmt.core import percent


class T(unittest.TestCase):
    def test_half(self):
        self.assertEqual(percent(1, 2), '50.0%')
