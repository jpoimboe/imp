from __future__ import annotations

import pathlib

WF = pathlib.Path("src/imp/assets/workflows/default")
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
        "batch-code-reviewer.md",
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


def test_imp_conventions_has_visual_and_plainspeak_sections() -> None:
    body = pathlib.Path("docs/internals/conventions.md").read_text()
    assert "## Visual identity" in body
    assert "## Plain-speak copy" in body
    assert "imp-native" in body
    assert '"express"' in body
    assert "front-door" not in body


def test_imp_invariants_has_layering_and_future_sections() -> None:
    body = pathlib.Path("docs/internals/invariants.md").read_text()
    assert "## Layering invariants" in body
    assert "## Future / unbuilt" in body
    assert "$IMP_TEST_COMMAND" in body
    assert "$IMP_LINT_COMMAND" in body
    assert "generic primitives" in body
    assert "Memory retention" in body
    assert "user-level" in body


def test_imp_operator_notes_has_unblocking_section() -> None:
    body = pathlib.Path("docs/guide/operator-notes.md").read_text()
    assert "## Unblocking a paused quest" in body
    assert "snapshot" in body
    assert "cherry-pick" in body
    assert "skew deadlock" in body.lower()
    # the local layer is the MAIN CHECKOUT's — a worktree .imp/config.yaml is inert
    assert "`<main checkout>/.imp/config.yaml`" in body
    assert "<worktree>/.imp/config.yaml" not in body


def test_memory_code_comments_present_at_sites() -> None:
    drift = pathlib.Path("tests/test_cc_builtins_drift.py").read_text()
    assert "Do NOT skip on CLAUDECODE=1" in drift

    openai = pathlib.Path("src/imp/providers/openai.py").read_text()
    assert "no encrypted" in openai
    assert "the latter 400s" in openai

    cc = pathlib.Path("src/imp/providers/claude_code.py").read_text()
    assert "blocks servers sourced from .mcp.json" in cc

    codex = pathlib.Path("src/imp/providers/codex_mcp.py").read_text()
    assert "not a posture reversal" in codex
