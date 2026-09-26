Layered config loading loses nested settings and corrupts the defaults

With defaults `{"db": {"host": "localhost", "port": 5432}}` and an override file containing
`{"db": {"port": 6543}}`, `load_layers([...])` returns `{"db": {"port": 6543}}` — the `host` key
is gone. After calling it, the `DEFAULTS` dictionary in `confmerge.loader` has also been modified,
so later loads start from the wrong defaults.

Nested mappings should be merged recursively, and loading must never modify its inputs.
