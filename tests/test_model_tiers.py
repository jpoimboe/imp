# tests/test_model_tiers.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.model_tiers import effective_model_tiers, parse_model_tiers
from imp.engine.types import SourceLoc

_SRC = SourceLoc(file=Path("w.yaml"), line=1)


def test_parse_none_is_empty() -> None:
    assert parse_model_tiers(None, _SRC) == ()


def test_parse_empty_list_is_empty() -> None:
    assert parse_model_tiers([], _SRC) == ()


def test_parse_orders_preserved() -> None:
    assert parse_model_tiers(["fast", "balanced", "powerful"], _SRC) == (
        "fast",
        "balanced",
        "powerful",
    )


def test_parse_non_list_raises() -> None:
    with pytest.raises(LoadError, match="must be a list"):
        parse_model_tiers("fast", _SRC)


def test_parse_non_string_entry_raises() -> None:
    with pytest.raises(LoadError, match="must be a string"):
        parse_model_tiers(["fast", 3], _SRC)


def test_parse_duplicate_raises() -> None:
    with pytest.raises(LoadError, match="duplicate"):
        parse_model_tiers(["fast", "fast"], _SRC)


class _Cfg:
    def __init__(self, model_tiers):
        self.model_tiers = model_tiers


class _Wf:
    def __init__(self, model_tiers):
        self.model_tiers = model_tiers


def test_effective_uses_workflow_when_config_absent() -> None:
    wf = _Wf(("fast", "powerful"))
    cfg = _Cfg(None)
    assert effective_model_tiers(wf, cfg) == ("fast", "powerful")


def test_effective_config_replaces_wholesale() -> None:
    wf = _Wf(("fast", "balanced", "powerful"))
    cfg = _Cfg(("balanced",))
    assert effective_model_tiers(wf, cfg) == ("balanced",)


def test_effective_config_empty_list_turns_feature_off() -> None:
    wf = _Wf(("fast", "balanced", "powerful"))
    cfg = _Cfg(())  # explicit empty list = off
    assert effective_model_tiers(wf, cfg) == ()
