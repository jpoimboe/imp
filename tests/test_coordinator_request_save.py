from __future__ import annotations

from imp.cli.signals import InterruptCoordinator


def test_request_save_invokes_registered_hook() -> None:
    coord = InterruptCoordinator()
    seen: list[str | None] = []
    coord.set_save_hook(lambda reason: seen.append(reason))
    coord.request_save(reason="save_and_exit")
    assert seen == ["save_and_exit"]


def test_request_save_default_reason_is_none() -> None:
    coord = InterruptCoordinator()
    seen: list[str | None] = []
    coord.set_save_hook(lambda reason: seen.append(reason))
    coord.request_save()
    assert seen == [None]


def test_request_save_without_hook_is_noop() -> None:
    # Must not raise when no hook has been registered (legacy/test contexts).
    InterruptCoordinator().request_save(reason="x")
