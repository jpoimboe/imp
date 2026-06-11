from __future__ import annotations

import imp.cli.main as m


def test_build_parser_new_accepts_tui_flag() -> None:
    ns = m._build_parser().parse_args(["new", "--tui", "a title"])
    assert ns.tui is True


def test_build_parser_new_tui_defaults_false() -> None:
    ns = m._build_parser().parse_args(["new"])
    assert ns.tui is False


def test_build_parser_resume_accepts_tui_flag() -> None:
    ns = m._build_parser().parse_args(["resume", "2026-06-11-x", "--tui"])
    assert ns.tui is True


def test_build_parser_resume_tui_defaults_false() -> None:
    ns = m._build_parser().parse_args(["resume", "2026-06-11-x"])
    assert ns.tui is False
