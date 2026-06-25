from __future__ import annotations

import pathlib

from imp.providers.config import parse_project_facts

WF = pathlib.Path("src/imp/assets/workflows/imp")
INCLUDE = '{% include "partials/verifying-claims.md" %}'


def test_verifying_claims_partial_has_novel_nugget() -> None:
    body = (WF / "partials" / "verifying-claims.md").read_text()
    assert "read the construction" in body
    assert "citation is not proof" in body
    assert "Absence in a log" in body


def test_verifying_claims_included_in_exactly_the_four_reviewers() -> None:
    expected = {
        "spec-reviewer.md",
        "plan-reviewer.md",
        "plan-code-reviewer.md",
        "code-reviewer.md",
    }
    including = {p.name for p in WF.rglob("*.md") if INCLUDE in p.read_text()}
    assert including == expected


def test_verifying_claims_excluded_from_task_lead_coder_and_skill() -> None:
    for rel in (
        "agents/task-lead.md",
        "agents/coder.md",
        "skills/verification-before-completion.md",
    ):
        assert INCLUDE not in (WF / rel).read_text(), rel


def test_project_yaml_guidance_loads_with_prerelease_and_pointers() -> None:
    _facts, guidance = parse_project_facts(pathlib.Path("imp/project.yaml"))
    assert isinstance(guidance, str)
    assert "pre-release" in guidance
    assert "Skip backward-compatibility" in guidance
    assert "docs/imp-conventions.md" in guidance
    assert "docs/imp-invariants.md" in guidance


def test_imp_conventions_has_visual_and_plainspeak_sections() -> None:
    body = pathlib.Path("docs/imp-conventions.md").read_text()
    assert "## Visual identity" in body
    assert "## Plain-speak copy" in body
    assert "imp-native" in body
    assert '"express"' in body


def test_imp_invariants_has_layering_and_future_sections() -> None:
    body = pathlib.Path("docs/imp-invariants.md").read_text()
    assert "## Layering invariants" in body
    assert "## Future / unbuilt" in body
    assert "$IMP_TEST_COMMAND" in body
    assert "$IMP_LINT_COMMAND" in body
    assert "generic primitives" in body
    assert "Memory retention" in body
    assert "user-level" in body


def test_imp_operator_notes_has_unblocking_section() -> None:
    body = pathlib.Path("docs/imp-operator-notes.md").read_text()
    assert "## Unblocking a paused change" in body
    assert "snapshot" in body
    assert "cherry-pick" in body
    assert "skew deadlock" in body.lower()
