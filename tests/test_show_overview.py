from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import (
    _build_parser,
    _cmd_list,
    _cmd_show,
    _command_names,
    main,
    render_overview,
)
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_list_no_arg_renders_overview(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", change_id="liveid", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["list"])
    out = buf.getvalue()
    assert rc == 0
    assert "NAME" in out          # overview header rendered
    assert "liveid" in out


def test_list_all_renders_history_block(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m
    from tests.test_journal_read_cli import _complete_and_archive

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Done", change_id="doneid", base_branch="main")
    _complete_and_archive(tmp_path, "doneid")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["list", "-a"])
    out = buf.getvalue()
    assert rc == 0
    assert "history" in out
    assert "doneid" in out and "merged" in out


def test_bare_show_nudges_to_list(capsys) -> None:
    rc = main(["show"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err == (
        "imp: 'imp show' needs a change id — run 'imp list' for an overview\n"
    )


def test_show_rejects_all_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["show", "liveid", "--all"])
    assert exc.value.code == 2


def test_list_and_show_both_resolve() -> None:
    parser = _build_parser()
    names = _command_names(parser)
    assert "list" in names and "show" in names
    assert parser.parse_args(["list"]).func is _cmd_list
    assert parser.parse_args(["show", "liveid"]).func is _cmd_show


def test_overview_lists_list_then_show_with_copy() -> None:
    text = render_overview(color=False)
    assert "List your changes: in-flight, backlog, and recently merged." in text
    assert "Show one change's detail or a named artifact (imp show <id>)." in text
    assert text.index("List your changes") < text.index("Show one change's detail")


def test_show_status_word_errors_not_found(tmp_path: Path, monkeypatch, capsys) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", "status"])     # `status` parsed as a token, not a resource
    assert rc == 2
    assert "no Change matches" in capsys.readouterr().err


def test_detail_card_routes_through_pager(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", change_id="liveid", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    captured: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        m, "_page_or_print",
        lambda lines, *, no_pager: captured.append((list(lines), no_pager)))

    rc = main(["show", "liveid"])
    assert rc == 0
    assert len(captured) == 1
    assert captured[0][0]                 # the detail card lines were paged


def test_resource_routes_through_pager(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", change_id="liveid", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    captured: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        m, "_page_or_print",
        lambda lines, *, no_pager: captured.append((list(lines), no_pager)))

    rc = main(["show", "liveid", "status"])
    assert rc == 0
    assert len(captured) == 1
    assert captured[0][0]                 # the resource lines were paged


def test_no_pager_flag_propagates_to_pager(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", change_id="liveid", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    captured: list[bool] = []
    monkeypatch.setattr(
        m, "_page_or_print",
        lambda lines, *, no_pager: captured.append(no_pager))

    main(["show", "liveid"])
    main(["show", "liveid", "--no-pager"])
    assert captured == [False, True]
