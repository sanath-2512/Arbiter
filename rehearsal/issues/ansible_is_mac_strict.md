Add a strict mode to is_mac() that rejects a trailing newline

`ansible.module_utils.common.network.is_mac('52:54:00:e1:44:e0\n')` returns True, so values read from files
or command output pass validation with a trailing newline still attached. Please add an opt-in,
keyword-only `strict` argument: `is_mac(value, strict=True)` should reject anything after the address,
including a trailing newline, while the default behaviour stays exactly as it is so existing callers keep
working. Both `:` and `-` separated forms (and upper case) remain valid; mixed separators are not.
