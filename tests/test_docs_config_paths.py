"""No doc, README, bundled asset prompt, or playbook may name the pre-IMP_HOME
global config location, the XDG knob that used to locate it, or the project
config file's pre-rename name; and no live doc may place per-project runtime
state inside the checkout."""
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


_LIVE_DOC_ROOTS = ("docs/guide", "docs/internals")

# Checkout-relative state paths. `.imp/quests/` (plural, the worktree symlink) is
# deliberately absent: docs still name it, and criteria 10 and 16 keep it correct.
_IN_CHECKOUT_STATE = (".imp/quest/", ".imp/worktrees", ".imp/archive")


@pytest.mark.parametrize("needle", _IN_CHECKOUT_STATE)
def test_no_live_doc_places_project_state_in_the_checkout(needle: str) -> None:
    offenders = sorted(
        f"{p}:{n}"
        for root in _LIVE_DOC_ROOTS
        for p in sorted(Path(root).rglob("*.md"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if needle in line)
    assert offenders == []


def test_playbooks_verify_against_the_bracketed_layer_label() -> None:
    """`imp config` tags each leaf with a bracketed short label; the retired
    `…/<path> (layer)` form must not survive in the verification steps."""
    for name in ("interrupt-playbook.md", "dogfood-playbook.md",
                 "imp-ux-playbook.md", "remote-control-playbook.md"):
        body = (Path("tests/playbooks") / name).read_text()
        assert "[~/.imp/projects/<clone-name>/config.yaml]" in body, name
        assert "[.imp/config.yaml]" not in body, name
        assert "(local)" not in body, name
        assert "(global)" not in body, name
