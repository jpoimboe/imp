"""No doc, README, skill, workflow prompt, or playbook may name the pre-IMP_HOME
global config location or the XDG knob that used to locate it."""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOTS = ("docs", "src/imp/assets/skills", "src/imp/assets/workflows",
          "tests/playbooks")


def _corpus() -> list[Path]:
    files = [Path("README.md")]
    for root in _ROOTS:
        found = sorted(p for p in Path(root).rglob("*.md") if p.is_file())
        assert found, f"no markdown files found under {root!r}"
        files.extend(found)
    return files


@pytest.mark.parametrize(
    "needle", ["~/.config/imp", "XDG_CONFIG_HOME", "(user)", "user config"])
def test_no_legacy_global_config_reference(needle: str) -> None:
    offenders = sorted(str(p) for p in _corpus() if needle in p.read_text())
    assert offenders == []
