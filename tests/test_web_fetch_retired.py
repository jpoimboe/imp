from __future__ import annotations

import importlib

import pytest

from imp.agents.tools import get_registry


def test_web_fetch_not_in_registry() -> None:
    assert "web_fetch" not in get_registry()


def test_web_fetch_module_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("imp.agents.tools.web_fetch")
