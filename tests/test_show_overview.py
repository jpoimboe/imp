from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from datetime import UTC, datetime
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
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from tests.conftest import _seed_control

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _term(monkeypatch, cols):
    monkeypatch.setattr("shutil.get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((cols, 24)))


def _inflight_home(control, cid, *, title, phase_label, step_label,
                   step="write", intent=None):
    cdir = control / ".imp" / "quests" / cid
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": cid, "title": title, "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00",
        "materialized": True, "layout_version": LAYOUT_VERSION}))
    evs = [{"type": "step_started", "agent_id": None,
            "timestamp": "2026-06-20T11:00:01+00:00",
            "payload": {"step": step, "phase": "p", "phase_label": phase_label}}]
    if intent is not None:
        evs.insert(0, {"type": "opening_intent", "agent_id": None,
                       "timestamp": "2026-06-20T11:00:00+00:00",
                       "payload": {"text": intent}})
    (cdir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
    State(id=cid, status="running", step=step, step_attempt=0,
          step_label=step_label, phase_label=phase_label).save(cdir / "state")


def test_list_no_arg_renders_overview(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
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
    Quest.create(tmp_path, title="Done", quest_id="doneid", base_branch="main")
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
    assert "no Quest matches" in capsys.readouterr().err


def test_detail_card_routes_through_pager(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
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
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
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
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    captured: list[bool] = []
    monkeypatch.setattr(
        m, "_page_or_print",
        lambda lines, *, no_pager: captured.append(no_pager))

    main(["show", "liveid"])
    main(["show", "liveid", "--no-pager"])
    assert captured == [False, True]


def test_overview_shows_full_phase_and_step_on_wide_terminal(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    _inflight_home(tmp_path, "phid01", title="Widget",
                   phase_label="implementation phase · plan 1/3 · task 1/5",
                   step_label="coding")
    _term(monkeypatch, 130)
    out = "\n".join(m._overview_lines(tmp_path, show_all=False, now=_NOW, color=False))
    assert "implementation phase · plan 1/3 · task 1/5 · coding" in out


def test_overview_header_aligns_with_rows(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    _inflight_home(tmp_path, "algn01", title="ZZZ",
                   phase_label="implementation phase · plan 1/3 · task 1/5",
                   step_label="coding")
    _term(monkeypatch, 80)
    lines = m._overview_lines(tmp_path, show_all=False, now=_NOW, color=False)
    header = lines[0]
    row = next(ln for ln in lines if ln.startswith("algn01"))
    # Same computed status_width -> NAME column starts at the same offset.
    assert header.index("NAME") == row.index("ZZZ")


def test_overview_subfloor_drops_suffix_no_overflow(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    intent = "please investigate the flaky integration test that keeps timing out on ci"
    _inflight_home(tmp_path, "subf01", title="", phase_label="spec phase",
                   step_label="writing spec", intent=intent)
    _term(monkeypatch, 80)
    lines = m._overview_lines(tmp_path, show_all=False, now=_NOW, color=False)
    header = lines[0]
    row = next(ln for ln in lines if ln.startswith("subf01"))
    assert "spec phase" not in row                    # suffix dropped
    assert "(" not in row.split("please")[0]          # no ` (…)` before NAME
    assert header.index("NAME") == row.index("please")  # cell width == status_width
