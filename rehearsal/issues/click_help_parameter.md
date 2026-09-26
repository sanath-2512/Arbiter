Move `help` handling from Option and Argument into Parameter

`Option` and `Argument` each implement their own copy of the `help` handling: dedenting the text with
`inspect.cleandoc`, appending the deprecation label (`(DEPRECATED)` or `(DEPRECATED: <message>)`) and
reporting it from `to_info_dict()`. The two copies are easy to let drift apart, and custom `Parameter`
subclasses cannot use them at all: `Parameter.__init__` does not accept `help`.

Please move this into `Parameter`: it should accept a `help` keyword, dedent it, add the deprecation label
when the parameter is deprecated, store it as `self.help`, and `Parameter.to_info_dict()` should report
it. `Option` and `Argument` should inherit that instead of duplicating it, and their observable behaviour
(help text, `--help` output, `to_info_dict()`) must stay the same.
