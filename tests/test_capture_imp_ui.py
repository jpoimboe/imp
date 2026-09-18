from __future__ import annotations

from datetime import UTC, datetime

import pytest

from imp.cli import splash
from scripts import capture_imp_ui


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
    events = capture_imp_ui.journal_events(capture_imp_ui._DEMO_JOURNAL, 40)

    assert len(events) == 40
    assert [event["ordinal"] for event in events] == list(range(1, 41))
    assert events[2]["type"] == "user_message"


def test_shift_lands_the_last_event_on_the_pinned_instant() -> None:
    events = capture_imp_ui.journal_events(capture_imp_ui._DEMO_JOURNAL, 40)
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
    events = capture_imp_ui.journal_events(capture_imp_ui._DEMO_JOURNAL, 40)
    instant = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    delta = int((instant - datetime.fromisoformat(
        events[-1]["timestamp"])).total_seconds())
    source = next(e for e in events if e["type"] == "quota_reading")

    moved = next(e for e in capture_imp_ui.shift_to_instant(events, instant)
                 if e["type"] == "quota_reading")

    assert ([w["resets_at"] for w in moved["payload"]["windows"]]
            == [w["resets_at"] + delta for w in source["payload"]["windows"]])
    # The caller's events are left untouched: the journal is read once per replay.
    assert source["payload"]["windows"][0]["resets_at"] == 1789714200
