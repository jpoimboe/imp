from __future__ import annotations

from imp.cli.signals import InterruptCoordinator


def test_runner_attached_saves_and_restores_previous() -> None:
    coord = InterruptCoordinator()

    async def parent_interrupt() -> None: ...
    async def child_interrupt() -> None: ...

    coord.attach_runner(parent_interrupt)
    assert coord._runner_interrupt is parent_interrupt

    with coord.runner_attached(child_interrupt):
        assert coord._runner_interrupt is child_interrupt

    # Restored, NOT cleared to None.
    assert coord._runner_interrupt is parent_interrupt


def test_runner_attached_restores_on_exception() -> None:
    coord = InterruptCoordinator()

    async def parent_interrupt() -> None: ...
    async def child_interrupt() -> None: ...

    coord.attach_runner(parent_interrupt)
    try:
        with coord.runner_attached(child_interrupt):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert coord._runner_interrupt is parent_interrupt


def test_runner_attached_from_empty_restores_none() -> None:
    coord = InterruptCoordinator()

    async def child_interrupt() -> None: ...

    with coord.runner_attached(child_interrupt):
        assert coord._runner_interrupt is child_interrupt
    assert coord._runner_interrupt is None
