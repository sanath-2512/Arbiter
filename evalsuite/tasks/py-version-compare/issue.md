pkgver fails to import on Python 3.12 (distutils removed)

`import pkgver` raises `ModuleNotFoundError: No module named 'distutils'` on Python 3.12+, because
`pkgver.compare` uses `distutils.version.LooseVersion`. Please remove the dependency on distutils
without adding third-party dependencies.

While doing so, make `compare_versions(a, b)` (returns -1, 0 or 1) handle pre-releases the way
packaging tools do: `1.0a1 < 1.0b2 < 1.0rc1 < 1.0 < 1.0.1`, and treat missing trailing
components as zero (`1.0 == 1.0.0`). Numeric components compare numerically (`1.10 > 1.9`).
