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


def test_imp_deferred_sweep_drains_deferred_md_append_only() -> None:
    sw = _imp("deferred-sweep.md")
    low = sw.lower()
    # Work source repointed to deferred.md.
    assert "**Deferred Followups** list in `{{ change.home }}/deferred.md`, not a plan's tasks" in sw
    assert "- `{{ change.home }}/deferred.md` — the **Deferred Followups** list (the work source)." in sw
    # Append-only: resolve by checking off, never deleting.
    assert "append-only" in low
    assert "check off" in low
    # The fixed disposition still records the durable opportunistic line in progress.md.
    assert "under `## Completed plans` in\n  `{{ change.home }}/progress.md`" in sw
    # The verify-triage toleration is recorded in deferred.md.
    assert "Record a toleration as a normal `## Deferred Followups` entry in\n`{{ change.home }}/deferred.md`" in sw


def test_imp_task_lead_files_ledger_and_forward_notes_to_deferred_md() -> None:
    tl = _imp("task-lead.md")
    # The deferred-finding append target is deferred.md.
    assert "under a\n   `## Deferred Followups` heading in the append-only `{{ change.home }}/deferred.md`" in tl
    # Forward notes are appended to deferred.md, never the rewritable progress.md.
    assert "by appending to `{{ change.home }}/deferred.md` — the" in tl
    assert "append-only ledger" in tl
    # The verify-triage toleration is recorded in deferred.md.
    assert "Record a toleration as a normal `## Deferred Followups` entry in\n`{{ change.home }}/deferred.md`" in tl
    # progress.md keeps the per-task commit-subject ledger and the Completed-plans collapse.
    assert "keep each task's **commit subject** in `progress.md`" in tl
    assert "it into a single `## Completed plans` line" in tl
    # The forward-note shapes still exist (they just live in deferred.md now).
    assert "- [ ] NEEDS-REPLAN task-lead:" in tl
    assert "- resolved-forward task-lead:" in tl
    # The step-5 lead-in no longer reads as if DEFERRED items go to progress.md.
    assert "(create it if missing) the **transient" in tl
    assert "does **not** live in" in tl
