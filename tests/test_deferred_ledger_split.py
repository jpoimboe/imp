from __future__ import annotations

from pathlib import Path

IMP = Path("src/imp/assets/workflows/imp/agents")
FLAT = Path("src/imp/assets/workflows/imp-flat/agents")


def _imp(name: str) -> str:
    return (IMP / name).read_text()


def _flat(name: str) -> str:
    return (FLAT / name).read_text()


def test_imp_producers_file_deferred_items_to_deferred_md() -> None:
    pw = _imp("plan-writer.md")
    # The "Surfacing a decision" append target moved off progress.md.
    assert "`{{ change.home }}/deferred.md` (create the `## Deferred Followups` heading if absent)" in pw
    # The ask-vs-decide scope-deviation surface moved too.
    assert "open item in `{{ change.home }}/deferred.md` (the reader" in pw
    # The forward-note (NEEDS-REPLAN) lifecycle handle is read from deferred.md.
    assert "note in `{{ change.home }}/deferred.md` (a `task-lead` needs-replan that" in pw

    pcr = _imp("plan-code-reviewer.md")
    assert "**Deferred Followups** list in `{{ change.home }}/deferred.md` so the" in pcr
    assert "`{{ change.home }}/deferred.md` (the Deferred Followups append above)" in pcr
    # progress.md is now described as completed tasks only; the ledger is its own file.
    assert "**Deferred ledger:** `{{ change.home }}/deferred.md`" in pcr

    rec = _imp("reconciler.md")
    assert "`{{ change.home }}/deferred.md` in reader form (create the file/heading if absent):" in rec
    # The reconciler still NARRATES through progress.md (transient surface stays).
    assert "## Surface your work — `{{ change.home }}/progress.md`" in rec

    sf = _imp("spec-finalizer.md")
    assert "in `{{ change.home }}/deferred.md` (create the `## Deferred Followups` heading if" in sf

    prv = _imp("plan-reviewer.md")
    assert "Deferred Followup open item in `{{ change.home }}/deferred.md`:" in prv
