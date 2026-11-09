"""No doc, README, bundled asset prompt, or playbook may name the pre-IMP_HOME
global config location, the XDG knob that used to locate it, or the project
config file's pre-rename name; and no live doc may place per-project runtime
state inside the checkout."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._corpus import tracked

_ROOTS = ("docs", "src/imp/assets", "tests/playbooks")


def _corpus() -> list[Path]:
    found = tracked(*_ROOTS)
    assert found, f"no tracked markdown files found under {_ROOTS!r}"
    return [Path("README.md"), *found]


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
        for p in tracked(*_LIVE_DOC_ROOTS)
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


def test_playbooks_claim_the_clone_state_dir_with_a_local_write() -> None:
    """Bare `imp config` claims nothing, so setup claims through a deliberate
    `--local` write before checking the marker."""
    for name, theme in (("dogfood-playbook.md", "ansi"), ("interrupt-playbook.md", "ansi"),
                        ("remote-control-playbook.md", "ansi"),
                        ("imp-ux-playbook.md", "dark")):
        body = (Path("tests/playbooks") / name).read_text()
        claim = f'cd "$WT" && imp config set --local theme {theme}'
        assert claim in body, name
        assert body.index(claim) < body.index('test -f "$STATE/repo"'), name
        assert "imp config | sed -n '1p'" not in body, name
        assert "imp config >/dev/null" not in body, name
    interrupt = " ".join((Path("tests/playbooks") / "interrupt-playbook.md").read_text().split())
    assert "it claims its own state directory on the first `--local` write" in interrupt
