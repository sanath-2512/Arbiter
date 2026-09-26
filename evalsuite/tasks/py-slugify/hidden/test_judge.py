import unittest

from textkit import slugify


class Judge(unittest.TestCase):
    def test_accents(self):
        self.assertEqual(slugify("Crème Brûlée"), "creme-brulee")
        self.assertEqual(slugify("naïve café", sep="_"), "naive_cafe")

    def test_collapse_and_trim(self):
        self.assertEqual(slugify("  Hello,   World!! "), "hello-world")
        self.assertEqual(slugify("--a--b--"), "a-b")

    def test_existing(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
        self.assertEqual(slugify("a b", sep="_"), "a_b")
