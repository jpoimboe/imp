from __future__ import annotations

from imp.cli.main import _resume_live_framing


def test_not_live_lock_is_plain_resuming_with_no_notice() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=False, halt_condition=None)
    assert marker == "resuming"
    assert notice is None


def test_not_live_lock_ignores_halt_condition() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=False, halt_condition="waiting")
    assert marker == "resuming"
    assert notice is None


def test_live_lock_non_waiting_reads_already_running() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=True, halt_condition=None)
    assert marker == "already running"
    assert notice == (
        "quest feat is already running in a live session — attaching to it.")


def test_live_lock_error_halt_still_reads_already_running() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=True, halt_condition="error")
    assert marker == "already running"
    assert notice == (
        "quest feat is already running in a live session — attaching to it.")


def test_live_lock_waiting_reads_already_waiting() -> None:
    marker, notice = _resume_live_framing(
        "feat", lock_live=True, halt_condition="waiting")
    assert marker == "already waiting"
    assert notice == (
        "quest feat is already waiting for input — attaching to answer it.")


def test_notice_interpolates_the_quest_id() -> None:
    _marker, notice = _resume_live_framing(
        "zzz123", lock_live=True, halt_condition="waiting")
    assert notice == (
        "quest zzz123 is already waiting for input — attaching to answer it.")
