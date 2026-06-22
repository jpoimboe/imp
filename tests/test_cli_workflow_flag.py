from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from imp.cli.main import _async_cmd_new, _build_parser, _command_names
from tests.conftest import _seed_control


def test_new_parser_accepts_workflow_flag() -> None:
    args = _build_parser().parse_args(["new", "do a thing", "--workflow", "patent"])
    assert args.workflow == "patent"


def test_new_parser_workflow_defaults_none() -> None:
    args = _build_parser().parse_args(["new", "do a thing"])
    assert args.workflow is None


def test_update_workflow_command_is_removed() -> None:
    assert "update-workflow" not in _command_names(_build_parser())


def test_async_cmd_new_rejects_invalid_workflow_name(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(_async_cmd_new(
        argparse.Namespace(
            workflow="bad/name", description=None, backlog=None,
        )))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
