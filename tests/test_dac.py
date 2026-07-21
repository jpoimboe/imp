from __future__ import annotations

import platform

import pytest

from tests._dac import dac_enforced


def test_dac_enforced_runs_body_and_returns() -> None:
    ran: list[bool] = []
    with dac_enforced():
        ran.append(True)
    assert ran == [True]


@pytest.mark.skipif(platform.system() != "Linux", reason="capget is Linux-only")
def test_dac_enforced_restores_effective_caps() -> None:
    from tests._dac import _capget

    before = _capget()[0].effective
    with dac_enforced():
        pass
    assert _capget()[0].effective == before


@pytest.mark.skipif(platform.system() != "Linux", reason="capget is Linux-only")
def test_dac_enforced_drops_target_caps_inside_when_held() -> None:
    from tests._dac import _MASK, _capget

    before = _capget()[0].effective
    if not (before & _MASK):
        pytest.skip("target caps not held: no-op path exercised by non-root runs")
    with dac_enforced():
        assert (_capget()[0].effective & _MASK) == 0
    assert _capget()[0].effective == before
