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


def test_playbooks_locate_quest_state_outside_the_clone() -> None:
    """Quest homes and worktrees live under the state directory; a playbook that
    still points inside `$WT` sends the operator to a path that does not exist."""
    for name in ("interrupt-playbook.md", "before-after-playbook.md"):
        body = (Path("tests/playbooks") / name).read_text()
        for stale in ("$WT/.imp/quests", "$REPO/.imp/quests", "$REPO/.imp/worktrees",
                      "<control_root>/.imp/quests", "`.imp/worktrees/<id>`"):
            assert stale not in body, f"{name}: {stale}"
    interrupt = (Path("tests/playbooks") / "interrupt-playbook.md").read_text()
    assert 'STATE="${IMP_HOME:-$HOME/.imp}/projects/$(basename "$WT")"' in interrupt
    before_after = (Path("tests/playbooks") / "before-after-playbook.md").read_text()
    assert 'imp show "$ID" --json' in before_after
    assert 'git -C "$REPO" worktree list --porcelain' in before_after


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


_RECLAIM = ('[ -d "$WT" ] && [ "$(cat "$STATE/repo" 2>/dev/null)" = "$(cd "$WT" && pwd -P)" ] '
            '&& rm -rf "$STATE"')


def test_playbook_cleanup_reclaims_the_project_state_directory() -> None:
    """A run's quests, worktrees and archive live outside the clone now, so a
    Cleanup block that only removes `$WT` strands them in the operator's home —
    and the marker guard keeps the removal to the directory this clone claimed."""
    for name in ("dogfood-playbook.md", "imp-ux-playbook.md",
                 "interrupt-playbook.md", "remote-control-playbook.md"):
        body = (Path("tests/playbooks") / name).read_text()
        assert _RECLAIM in body, name
        # the state dir goes first: the guard reads `$WT` to confirm ownership
        assert body.index(_RECLAIM) < body.index('rm -rf "$WT"'), name
