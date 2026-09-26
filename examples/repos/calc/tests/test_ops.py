import unittest

from calc import add, divide, mean


class OpsTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_divide_exact(self):
        self.assertEqual(divide(6, 3), 2)

    def test_mean_of_ints(self):
        self.assertEqual(mean([2, 4]), 3)


if __name__ == "__main__":
    unittest.main()
