import unittest

from textkit import slugify


class SlugTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_sep(self):
        self.assertEqual(slugify("a b", sep="_"), "a_b")


if __name__ == "__main__":
    unittest.main()
