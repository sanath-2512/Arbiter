Add time-based expiry to LRUCache

Please add an optional time-to-live to `cachekit.LRUCache`:

    LRUCache(maxsize, ttl=None, clock=time.monotonic)

- `ttl` is a number of seconds; `None` (default) means entries never expire (current behaviour).
- An entry expires `ttl` seconds after it was last *set*. Expired entries behave exactly like
  missing ones: `get` returns the default, `key in cache` is False, and `len(cache)` does not count them.
- `clock` is a zero-argument callable returning the current time in seconds, so tests can control time.
- LRU eviction by `maxsize` must keep working as before.
