import re
from pathlib import Path

from imp.agents.permissions import _glob_to_regex
from imp.agents.tools import get_registry
from imp.agents.tools.limits import leads_to_or_within_artifacts, within_artifacts

WORKFLOW = Path("src/imp/assets/workflows/imp/workflow.yaml")


def test_within_artifacts_matches_relocated_home() -> None:
    assert within_artifacts(Path(".imp/changes/abc123/artifacts"))
    assert within_artifacts(Path(".imp/changes/abc123/artifacts/spec/x.md"))


def test_within_artifacts_rejects_legacy_and_siblings() -> None:
    assert not within_artifacts(Path(".imp/change/artifacts"))        # legacy worktree home
    assert not within_artifacts(Path(".imp/changes/abc123/state"))    # sibling, not artifacts
    assert not within_artifacts(Path(".imp/changes/abc123"))          # the home dir itself


def test_leads_to_artifacts_covers_ancestors() -> None:
    assert leads_to_or_within_artifacts(Path(".imp"))
    assert leads_to_or_within_artifacts(Path(".imp/changes"))
    assert leads_to_or_within_artifacts(Path(".imp/changes/abc123"))
    assert leads_to_or_within_artifacts(Path(".imp/changes/abc123/artifacts/plans"))


def test_leads_to_artifacts_rejects_unrelated() -> None:
    assert not leads_to_or_within_artifacts(Path(".git"))
    assert not leads_to_or_within_artifacts(Path(".imp/changes/abc123/state"))


def test_changes_glob_matches_relocated_spec_home() -> None:
    rx = _glob_to_regex(".imp/changes/*/artifacts/spec/**")
    assert rx.match(".imp/changes/abc123/artifacts/spec/my-handle.md")
    assert rx.match(".imp/changes/abc123/artifacts/spec/nested/deep.md")
    assert not rx.match(".imp/change/artifacts/spec/my-handle.md")    # legacy worktree path


def test_shipped_workflow_has_no_legacy_change_dir() -> None:
    # `.imp/change` not followed by 's' catches the legacy worktree home while
    # allowing the relocated `.imp/changes/` pattern.
    assert not re.search(r"\.imp/change(?!s)", WORKFLOW.read_text())


def test_glob_grep_descriptions_drop_legacy_change_dir() -> None:
    reg = get_registry()
    for name in ("glob", "grep"):
        assert ".imp/change/" not in reg[name].description
