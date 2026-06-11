from __future__ import annotations

import argparse

import anyio

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


class _NotTty:
    def isatty(self) -> bool:
        return False


class _Tty:
    def isatty(self) -> bool:
        return True


def test_warn_if_tui_unavailable_warns_on_non_tty(capsys) -> None:
    m._warn_if_tui_unavailable(argparse.Namespace(tui=True), _NotTty())
    assert "interactive terminal" in capsys.readouterr().err


def test_warn_if_tui_unavailable_silent_on_tty(capsys) -> None:
    m._warn_if_tui_unavailable(argparse.Namespace(tui=True), _Tty())
    assert capsys.readouterr().err == ""


def test_warn_if_tui_unavailable_silent_without_flag(capsys) -> None:
    m._warn_if_tui_unavailable(argparse.Namespace(tui=False), _NotTty())
    assert capsys.readouterr().err == ""


def test_new_tui_on_non_tty_warns_and_streams(tmp_path, monkeypatch, capsys) -> None:
    from imp.engine.executor import RunResult
    from tests.conftest import _seed_control

    _seed_control(tmp_path)

    async def fake_run_change(change, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": change.slug, "title": change.title}})
        on_event({"id": "2", "agent_id": None, "type": "step_started",
                  "payload": {"step": "s", "kind": "agent", "phase": "design",
                              "model": "x", "provider": "anthropic"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    # Under pytest's capture, sys.stdout.isatty() is False → the non-tty path.
    args = argparse.Namespace(title="demo", tui=True)
    rc = anyio.run(m._async_cmd_new, args)
    out, err = capsys.readouterr()
    assert rc == 0
    assert "interactive terminal" in err     # warning surfaced on stderr
    assert "design" in out                    # still streamed via LiveDriver
