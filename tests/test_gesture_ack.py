# tests/test_gesture_ack.py
from __future__ import annotations

import asyncio

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


def test_signal_pause_twice_acks_once() -> None:
    # Pins the first-gesture WIRING: signal_pause guards the ack on
    # `pause_reason is None`, so a repeated stop must NOT re-ack. (The unit tests
    # above call _note_gesture_ack directly and don't exercise this guard.)
    coord = InterruptCoordinator()
    acks: list[str] = []
    coord.set_ack_hook(acks.append)
    asyncio.run(coord.signal_pause(reason="ctrl-c"))
    asyncio.run(coord.signal_pause(reason="ctrl-c"))
    assert acks == ["stopping…"]  # fired exactly once on the first gesture
