import unittest

from pkgver import compare_versions, is_newer


class CompareTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(compare_versions("1.2", "1.10"), -1)
        self.assertTrue(is_newer("2.0", "1.9.9"))


if __name__ == "__main__":
    unittest.main()
