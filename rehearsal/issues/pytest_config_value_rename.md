Rename IniValue to ConfigValue

`_pytest.config.findpaths.IniValue` holds a configuration value together with its origin (`"file"` or
`"override"`). With native TOML configuration support, values no longer come only from ini files, so the
name is misleading. Please rename the class to `ConfigValue`: same fields, same frozen-dataclass
behaviour (equality, hashing), `ConfigDict` maps names to `ConfigValue`, and every place that creates or
refers to these values (config file loading, `-o/--override-ini` parsing, `Config` value lookup) uses the
new name. Update comments and docstrings that describe it. Nothing observable other than the name should
change.
