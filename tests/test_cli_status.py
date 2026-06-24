from __future__ import annotations

import argparse
from pathlib import Path

import imp.cli.main as m
from imp.cli.main import _cmd_status, _status_rows
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _git, _seed_control


def test_status_lists_inflight_worktrees(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    rows = _status_rows(tmp_path)
    slugs = {r[0] for r in rows}
    assert slugs == {"alpha", "beta"}


def test_status_rows_include_errored_and_carry_iso(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    ch = Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    # Flip beta to errored: it must still appear in the in-flight default view.
    st = State.load(ch.state_dir)
    st.status = "errored"
    st.save(ch.state_dir)
    # Give alpha a known last event so its row carries a raw ISO timestamp.
    ch_a = Change.load(tmp_path, "alpha")
    ch_a.events_path.write_text('{"timestamp":"2026-06-05T12:00:00+00:00"}\n')

    rows = {r[0]: r for r in _status_rows(tmp_path)}
    assert set(rows) == {"alpha", "beta"}          # errored beta is in-flight
    assert rows["beta"][1] == "errored"
    assert rows["alpha"][3] == "2026-06-05T12:00:00+00:00"   # r[3] is raw ISO, not local


def test_create_seeds_step_label(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    st = State.load(ch.state_dir)
    flat0_name = st.step                       # the seeded initial cursor
    # The stub workflow's first step has no label:, so step_label falls back to the
    # step NAME — but it must be set (not None), proving the seed wrote it.
    assert flat0_name is not None
    assert st.step_label == flat0_name


def test_status_row_carries_step_label_as_seventh_element(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    row = next(r for r in _status_rows(tmp_path) if r[0] == "alpha")
    assert len(row) == 7
    assert row[6] == row[2]    # stub step has no label → step_label == step name


def test_backlog_rows_carry_provenance_and_empty_step_when_ready(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Brief", change_id="bk1",
                        base_branch="main", origin="idea", seed_opening="Brief")
    row = next(r for r in m._backlog_rows(tmp_path) if r[0] == "bk1")
    assert row[1] == "idea"       # provenance in the status slot
    assert row[2] == ""           # ready ⇒ empty step slot, no suffix
    assert row[6] is None


def test_cmd_status_merges_status_and_renders_bare_backlog(
    tmp_path: Path, capsys,
) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    Change.create_draft(tmp_path, title="Brief", change_id="bk1",
                        base_branch="main", origin="idea", seed_opening="Brief")
    rc = _cmd_status(argparse.Namespace(all=False), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "running (s)" in out                      # merged Active status + step label
    backlog_line = next(ln for ln in out.splitlines() if ln.startswith("bk1"))
    assert "idea" in backlog_line
    assert "(" not in backlog_line                   # ready ⇒ bare word, no suffix


def test_cmd_status_shows_intent_for_empty_title(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="", change_id="alpha", base_branch="main")
    ch.event_log().append(agent_id=None, type="opening_intent",
                          payload={"text": "add a dark mode toggle"})
    rc = _cmd_status(argparse.Namespace(all=False), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "add a dark mode toggle" in out
    assert "(unnamed)" not in out


def test_cmd_status_intent_falls_back_to_user_message(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="", change_id="alpha", base_branch="main")
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user",
                 "content": [{"type": "text", "text": "fix the flaky test"}]})
    _cmd_status(argparse.Namespace(all=False), tmp_path)
    out = capsys.readouterr().out
    assert "fix the flaky test" in out


def test_cmd_status_named_row_skips_events_load(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Named", change_id="alpha", base_branch="main")

    def boom(*a, **k):
        raise AssertionError("events loaded for a named row")

    monkeypatch.setattr(m, "_load_events_for_id", boom)
    rc = _cmd_status(argparse.Namespace(all=False), tmp_path)
    assert rc == 0


def test_create_draft_seeds_raw_templated_label(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    wf = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "vars:\n  k: {type: int, default: 7}\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/stub.md, label: \"seeding {{ vars.k }}\"}\n"
    )
    ch = Change.create_draft(tmp_path, title="A", change_id="draft1", base_branch="main")
    st = State.load(ch.state_dir)
    # No render at create time (change/log unavailable): the raw template is seeded,
    # NOT "seeding 7".
    assert st.step_label == "seeding {{ vars.k }}"


def test_create_seeds_raw_templated_label(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    wf = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "vars:\n  k: {type: int, default: 7}\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/stub.md, label: \"seeding {{ vars.k }}\"}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "add templated label")
    ch = Change.create(tmp_path, title="A", change_id="crttpl", base_branch="main")
    st = State.load(ch.state_dir)
    assert st.step_label == "seeding {{ vars.k }}"


def test_cmd_status_renders_draft_in_active(
    tmp_path: Path, capsys,
) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Draftish", change_id="d1",
                        base_branch="main")          # un-materialized, origin "draft"
    rc = _cmd_status(argparse.Namespace(all=False), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Backlog" not in out          # a lone draft creates no Backlog section
    d1_line = next(ln for ln in out.splitlines() if ln.startswith("d1"))
    assert "running" in d1_line          # a live draft shows running in Active
