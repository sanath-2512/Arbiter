"""Lab-authored test (Judge Rehearsal Lab): `Parameter` owns the help handling, so a third-party
parameter kind gets the same dedenting, deprecation label and info-dict entry as Option and Argument."""

import pytest

import click


class _CustomParameter(click.Parameter):
    param_type_name = "custom"

    def _parse_decls(self, decls, expose_value):
        return (decls[0] if decls else None), list(decls), []

    def add_to_parser(self, parser, ctx):
        pass


@pytest.mark.parametrize(
    ("help_in", "deprecated", "help_out"),
    [
        pytest.param("Pack the basket.", False, "Pack the basket.", id="plain"),
        pytest.param("\n    Pack the\n    basket.\n    ", False, "Pack the\nbasket.", id="dedent"),
        pytest.param(None, True, "(DEPRECATED)", id="label-only"),
        pytest.param(
            "Pack the basket.",
            "USE THE CRATE",
            "Pack the basket. (DEPRECATED: USE THE CRATE)",
            id="custom-label",
        ),
    ],
)
def test_custom_parameter_help(help_in, deprecated, help_out):
    param = _CustomParameter(["name"], help=help_in, deprecated=deprecated)
    assert param.help == help_out
    assert param.to_info_dict()["help"] == help_out


def test_custom_parameter_without_help():
    param = _CustomParameter(["name"])
    assert param.help is None
    assert param.to_info_dict()["help"] is None
