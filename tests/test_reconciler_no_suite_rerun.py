from pathlib import Path

WF = Path("src/imp/assets/workflows/default")


def test_reconciler_guard_names_lint_and_typecheck() -> None:
    body = (WF / "agents" / "reconciler.md").read_text()
    # The prohibition now names the full suite, lint, AND typecheck (not the suite alone).
    assert "full suite, lint, or typecheck" in body
    # It still attributes the combined checks to integration-verify running right after.
    assert "integration-verify" in body
    assert "the very next step" in body
    assert "the combined checks" in body
    # The existing targeted/narrowest-tests allowance is retained.
    assert "targeted/narrowest test to confirm a specific fix" in body
    # The stale suite-only phrasing is gone.
    assert "full/slow suite" not in body
