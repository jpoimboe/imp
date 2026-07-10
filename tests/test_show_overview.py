from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from imp.cli.main import _build_parser, _cmd_show, _command_names, main, render_overview
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_show_no_arg_renders_overview(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", change_id="liveid", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["show"])
    out = buf.getvalue()
    assert rc == 0
    assert "NAME" in out          # overview header rendered
    assert "liveid" in out


def test_show_all_renders_history_block(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m
    from tests.test_journal_read_cli import _complete_and_archive

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Done", change_id="doneid", base_branch="main")
    _complete_and_archive(tmp_path, "doneid")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["show", "-a"])
    out = buf.getvalue()
    assert rc == 0
    assert "history" in out
    assert "doneid" in out and "merged" in out


def test_show_status_word_errors_not_found(tmp_path: Path, monkeypatch, capsys) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", "status"])     # `status` parsed as a token, not a resource
    assert rc == 2
    assert "no Change matches" in capsys.readouterr().err


def test_list_subcommand_is_gone(capsys) -> None:
    rc = main(["list"])
    assert rc == 2
    assert "list" in capsys.readouterr().err


def test_show_is_the_overview_verb_and_list_removed() -> None:
    names = _command_names(_build_parser())
    assert "show" in names
    assert "list" not in names
    assert _build_parser().parse_args(["show"]).func is _cmd_show
    assert "Show your changes" in render_overview(color=False)


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


def test_no_live_imp_list_references() -> None:
    """No source under src/imp may still reference the removed `imp list` verb
    (tests/playbooks are out of scope)."""
    root = Path(__file__).resolve().parents[1] / "src" / "imp"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in (".py", ".md")
        and "imp list" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], offenders
