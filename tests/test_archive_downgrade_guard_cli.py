"""imp CLI against an archive whose blob floor is ahead of this install.

Only the record/blob readers gate on the data floor now, so every live-id path —
which reads the live home, or the index — succeeds. An archived id still refuses,
at the record read instead of at id resolution."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import archive
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _seed_control

ARCH = "arqu99"
LIVE = "livid1"
BKLG = "bklg01"

_ARCH_EVENTS = json.dumps({
    "id": "e0", "parent_id": None, "agent_id": None,
    "timestamp": "2026-06-01T00:00:01+00:00", "type": "step_started",
    "payload": {"step": "spec"}}) + "\n"

_LIVE_EVENTS = (
    json.dumps({"id": "l0", "parent_id": None, "agent_id": None,
                "timestamp": "2026-06-02T00:00:01+00:00", "type": "step_started",
                "payload": {"step": "spec"}}) + "\n"
    + json.dumps({"id": "l1", "parent_id": "l0", "agent_id": None,
                  "timestamp": "2026-06-02T00:00:02+00:00", "type": "user_message",
                  "payload": {"role": "user", "subtype": "intent",
                              "content": [{"type": "text",
                                           "text": "quicksilver"}]}}) + "\n"
)


def _seed_newer_archive(control: Path) -> int:
    """One archived merged quest, then the blob floor stamped one ahead of this install."""
    events = control / "arch.jsonl"
    events.write_text(_ARCH_EVENTS)
    quest_json = control / "arch.quest.json"
    quest_json.write_text(json.dumps({
        "id": ARCH, "title": "Archived Thing", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / "arch.state.json"
    state_json.write_text(json.dumps({
        "id": ARCH, "status": "merged", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_quest(
        control, ARCH, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(control).write_text(str(ahead))
    return ahead


def _expected_stderr_line(ahead: int) -> str:
    """The error's own sentence, prefixed by _emit_cli_error's `imp: ` — nothing else.
    The exact wording is pinned in tests/test_archive_mismatch_error.py."""
    exc = ArchiveMismatch(floor="data", stored=ahead, known=archive._DATA_VERSION)
    return f"imp: {exc}"


def _arrange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """One live quest with real events, one backlog seed, one archived merged quest,
    and the blob floor one ahead."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live Thing", quest_id=LIVE, base_branch="main")
    (project_state_dir(tmp_path) / "quests" / LIVE / "events.jsonl").write_text(
        _LIVE_EVENTS)
    Quest.create_draft(
        tmp_path, title="Queued Thing", quest_id=BKLG, base_branch="main",
        origin="backlog", seed_opening="Queued Thing\n\nbody", defer_seed=True)
    ahead = _seed_newer_archive(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    return ahead


def test_list_renders_every_section_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 1(d): the journal comes from the index, which the ahead blob floor
    does not gate."""
    _arrange(tmp_path, monkeypatch)

    rc = main(["list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert LIVE in captured.out and "Live Thing" in captured.out
    assert "backlog:" in captured.out and BKLG in captured.out
    assert "completed:" in captured.out and ARCH in captured.out


def test_show_renders_a_live_quests_real_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arrange(tmp_path, monkeypatch)

    rc = main(["show", LIVE])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Live Thing" in captured.out
    assert "quicksilver" in captured.out       # the intent, read from the live home


def test_log_renders_a_live_quests_real_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arrange(tmp_path, monkeypatch)

    rc = main(["log", LIVE])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "quicksilver" in captured.out


def test_events_indexes_a_live_quests_real_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arrange(tmp_path, monkeypatch)

    rc = main(["events", LIVE])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "2 events" in captured.out
    assert "user_message" in captured.out


def test_show_surfaces_the_same_line_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """id resolution succeeds (it reads the index, which is current), then the
    record read refuses: no intermediate handler turns that refusal into a
    rehydrate fallback, an empty result, or 'no quest matches'."""
    ahead = _arrange(tmp_path, monkeypatch)

    rc = main(["show", ARCH])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.splitlines() == [_expected_stderr_line(ahead)]
    assert captured.out.strip() == ""
