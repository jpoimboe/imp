"""No doc, README, bundled asset prompt, or playbook may name the pre-IMP_HOME
global config location, the XDG knob that used to locate it, or the project
config file's pre-rename name."""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOTS = ("docs", "src/imp/assets", "tests/playbooks")


def _corpus() -> list[Path]:
    files = [Path("README.md")]
    for root in _ROOTS:
        found = sorted(p for p in Path(root).rglob("*.md") if p.is_file())
        assert found, f"no markdown files found under {root!r}"
        files.extend(found)
    return files


@pytest.mark.parametrize(
    "needle",
    ["~/.config/imp", "XDG_CONFIG_HOME", "(user)", "user config", "project.yaml"])
def test_no_retired_config_name_reference(needle: str) -> None:
    offenders = sorted(str(p) for p in _corpus() if needle in p.read_text())
    assert offenders == []


def test_playbooks_verify_against_the_bracketed_layer_label() -> None:
    """`imp config` tags each leaf with a bracketed short label; the retired
    `…/<path> (layer)` form must not survive in the verification steps."""
    for name in ("interrupt-playbook.md", "dogfood-playbook.md"):
        body = (Path("tests/playbooks") / name).read_text()
        assert "[.imp/config.yaml]" in body, name
        assert "(local)" not in body, name
        assert "(global)" not in body, name
