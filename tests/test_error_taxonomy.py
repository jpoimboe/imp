"""Fixes G/H: api + loop categories exist and map to Pause."""

from __future__ import annotations

from imp.engine import error_policy as ep
from imp.providers import errors as perr


def test_api_error_category() -> None:
    assert perr.ApiError("x").category == "api"


def test_loop_error_category() -> None:
    assert perr.LoopError("x").category == "loop"


def test_api_decides_pause() -> None:
    assert isinstance(ep.decide("api", prior_backoff_attempts=0), ep.Pause)


def test_loop_decides_pause() -> None:
    assert isinstance(ep.decide("loop", prior_backoff_attempts=0), ep.Pause)
