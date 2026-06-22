from __future__ import annotations

from imp.cli.main import _build_parser, _command_names


def test_new_parser_accepts_workflow_flag() -> None:
    args = _build_parser().parse_args(["new", "do a thing", "--workflow", "patent"])
    assert args.workflow == "patent"


def test_new_parser_workflow_defaults_none() -> None:
    args = _build_parser().parse_args(["new", "do a thing"])
    assert args.workflow is None


def test_update_workflow_command_is_removed() -> None:
    assert "update-workflow" not in _command_names(_build_parser())
