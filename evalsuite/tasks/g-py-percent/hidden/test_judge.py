import unittest

from fmt.core import percent


class Judge(unittest.TestCase):
    def test_round(self):
        self.assertEqual(percent(1, 3), '33.3%')
        self.assertEqual(percent(0, 0), '0.0%')
