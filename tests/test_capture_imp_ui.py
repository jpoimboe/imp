from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from imp.cli import splash
from scripts import capture_imp_ui
from tests._event_fixtures import _TTY


def test_scene_splash_all_scenes_labels_and_renders_the_complete_pool(capsys) -> None:
    capture_imp_ui.scene_splash(
        color=False, width=80, theme="dark", all_scenes=True)

    out = capsys.readouterr().out
    labels = [line for line in out.splitlines() if line.startswith("[")]
    assert labels == [
        f"[{index:02d}] {builder.__name__}"
        for index, (builder, _phrase) in enumerate(splash.SCENES)
    ]
    for _builder, phrase in splash.SCENES:
        assert phrase in out


def test_scene_splash_single_scene_keeps_unlabelled_output(capsys) -> None:
    capture_imp_ui.scene_splash(
        color=False, width=80, theme="dark", index=1)

    out = capsys.readouterr().out
    assert "[01]" not in out
    assert out.endswith(splash.SCENES[1][1] + "\n")


def test_all_scenes_rejects_non_splash_modes() -> None:
    with pytest.raises(SystemExit, match="2"):
        capture_imp_ui.main(["stream", "--all-scenes"])


def test_journal_events_reads_one_dict_per_line_and_honors_the_limit() -> None:
    events = capture_imp_ui.journal_events(capture_imp_ui.DEMO_JOURNAL, 40)

    assert len(events) == 40
    assert [event["ordinal"] for event in events] == list(range(1, 41))
    assert events[2]["type"] == "user_message"


def test_shift_lands_the_last_event_on_the_pinned_instant() -> None:
    events = capture_imp_ui.journal_events(capture_imp_ui.DEMO_JOURNAL, 40)
    instant = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    moved = capture_imp_ui.shift_to_instant(events, instant)

    assert datetime.fromisoformat(moved[-1]["timestamp"]) == instant
    # A uniform shift preserves every interval, so elapsed readings stay honest.
    source_span = (datetime.fromisoformat(events[-1]["timestamp"])
                   - datetime.fromisoformat(events[0]["timestamp"]))
    moved_span = (datetime.fromisoformat(moved[-1]["timestamp"])
                  - datetime.fromisoformat(moved[0]["timestamp"]))
    assert moved_span == source_span


def test_shift_moves_quota_reset_epochs_by_the_same_delta() -> None:
    events = capture_imp_ui.journal_events(capture_imp_ui.DEMO_JOURNAL, 40)
    instant = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    delta = round((instant - datetime.fromisoformat(
        events[-1]["timestamp"])).total_seconds())
    source = next(e for e in events if e["type"] == "quota_reading")

    moved = next(e for e in capture_imp_ui.shift_to_instant(events, instant)
                 if e["type"] == "quota_reading")

    assert ([w["resets_at"] for w in moved["payload"]["windows"]]
            == [w["resets_at"] + delta for w in source["payload"]["windows"]])
    # The caller's events are left untouched: the journal is read once per replay.
    assert source["payload"]["windows"][0]["resets_at"] == 1789714200


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_HERO_POSITION_ROW = (
    "samo71 · JSON output for imp list · implementation phase · "
    "plan 1/3 · task 1/3 · claude-opus-5[1m] · ██░░░░░░░░ 43k/200k")
_HERO_SUBAGENT_ROW = "↳ coder · 47s · claude-sonnet-4-6 · █░░░░░░░░░ 17k/160k"


def _screen_rows(text: str) -> list[str]:
    """Footer rows as a terminal shows them: escapes dropped, and each line cut
    to what survives its last carriage return."""
    return [_ANSI.sub("", line).split("\r")[-1].rstrip()
            for line in text.split("\n")]


def test_hero_frame_renders_the_complete_position_row() -> None:
    out = _TTY()

    capture_imp_ui.replay_live(out)

    rows = [row for row in _screen_rows(out.getvalue()) if row]
    assert rows[-2] == _HERO_POSITION_ROW
    assert rows[-1] == _HERO_SUBAGENT_ROW
    assert "…" not in rows[-2]


def test_hero_frame_is_independent_of_the_wall_clock(monkeypatch) -> None:
    from imp.cli import live_driver as ld

    def frame_at(now: datetime) -> str:
        class _Clock:
            @staticmethod
            def now(tz=None):
                return now

        class _Epoch:
            @staticmethod
            def time():
                return now.timestamp()

        monkeypatch.setattr(ld, "datetime", _Clock)
        monkeypatch.setattr(ld, "time", _Epoch)
        out = _TTY()
        capture_imp_ui.replay_live(out)
        return out.getvalue()

    assert (frame_at(datetime(2026, 3, 4, 11, 7, 13, tzinfo=UTC))
            == frame_at(datetime(2031, 11, 30, 23, 59, 1, tzinfo=UTC)))


def test_stream_live_defaults_to_the_committed_journal(capsys) -> None:
    assert capture_imp_ui.main(["stream-live", "--limit", "40", "--width", "124"]) == 0

    out = capsys.readouterr().out
    assert "add a --json flag to imp list" in out
    assert "loading skill imp-knowledge" in out
