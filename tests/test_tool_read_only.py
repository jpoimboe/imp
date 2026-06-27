from __future__ import annotations

from imp.agents.tools import get_registry


def test_read_only_true_on_read_tools() -> None:
    reg = get_registry()
    assert reg["read_file"].read_only is True
    assert reg["grep"].read_only is True
    assert reg["glob"].read_only is True


def test_read_only_false_on_mutating_tools() -> None:
    reg = get_registry()
    assert reg["write_file"].read_only is False
    assert reg["edit_file"].read_only is False
    assert reg["bash"].read_only is False
