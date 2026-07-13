from __future__ import annotations

from imp.cli.signals import InterruptCoordinator


def test_override_defaults_to_sandboxed() -> None:
    coord = InterruptCoordinator()
    assert coord.unsandboxed is False


def test_set_unsandboxed_flips_the_flag() -> None:
    coord = InterruptCoordinator()
    coord.set_unsandboxed(True)
    assert coord.unsandboxed is True
    coord.set_unsandboxed(False)
    assert coord.unsandboxed is False


def test_override_survives_a_gesture_round_reset() -> None:
    # A between-rounds reset() (interrupt bookkeeping) must NOT clear the override:
    # the flag's lifecycle is owned by the executor step-advance clear and the
    # daemon persist/restore, never by the per-round reset().
    coord = InterruptCoordinator()
    coord.set_unsandboxed(True)
    coord.reset()
    assert coord.unsandboxed is True
