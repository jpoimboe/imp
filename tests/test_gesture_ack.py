# tests/test_gesture_ack.py
from __future__ import annotations

from imp.cli.signals import InterruptCoordinator


def test_first_gesture_calls_ack_hook_with_stopping() -> None:
    coord = InterruptCoordinator()
    acks: list[str] = []
    coord.set_ack_hook(acks.append)
    coord._note_gesture_ack(force_quit_available=False)
    assert acks == ["stopping…"]


def test_ack_appends_force_quit_after_window() -> None:
    coord = InterruptCoordinator()
    acks: list[str] = []
    coord.set_ack_hook(acks.append)
    coord._note_gesture_ack(force_quit_available=True)
    assert acks == ["stopping… (interrupt again to force-quit)"]
