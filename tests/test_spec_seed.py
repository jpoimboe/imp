from __future__ import annotations

from imp.cli import main as cli_main


def test_build_parser_new_accepts_spec_file() -> None:
    ns = cli_main._build_parser().parse_args(["new", "--spec-file", "draft.md"])
    assert ns.spec_file == "draft.md"


def test_build_parser_new_spec_file_defaults_none() -> None:
    ns = cli_main._build_parser().parse_args(["new"])
    assert ns.spec_file is None
