import unittest

from cachekit import LRUCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Judge(unittest.TestCase):
    def test_expiry(self):
        clk = Clock()
        c = LRUCache(10, ttl=5, clock=clk)
        c.set("a", 1)
        clk.t += 4.9
        self.assertEqual(c.get("a"), 1)
        clk.t += 0.2
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.get("a", "d"), "d")
        self.assertNotIn("a", c)
        self.assertEqual(len(c), 0)

    def test_set_refreshes_but_get_does_not(self):
        clk = Clock()
        c = LRUCache(10, ttl=5, clock=clk)
        c.set("a", 1)
        clk.t += 3
        c.get("a")
        clk.t += 3
        self.assertNotIn("a", c)
        c.set("b", 1)
        clk.t += 3
        c.set("b", 2)
        clk.t += 3
        self.assertEqual(c.get("b"), 2)

    def test_len_excludes_expired(self):
        clk = Clock()
        c = LRUCache(10, ttl=1, clock=clk)
        c.set("a", 1)
        clk.t += 0.5
        c.set("b", 2)
        clk.t += 0.6
        self.assertEqual(len(c), 1)

    def test_default_no_ttl_and_lru(self):
        c = LRUCache(2)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")
        c.set("c", 3)
        self.assertEqual((("a" in c), ("b" in c), len(c)), (True, False, 2))
