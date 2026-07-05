from __future__ import annotations

from pathlib import Path

IMP = Path("src/imp/assets/workflows/imp/agents")


def _imp(name: str) -> str:
    return (IMP / name).read_text()


def test_imp_producers_file_deferred_items_to_deferred_md() -> None:
    pw = _imp("plan-writer.md")
    # The "Surfacing a decision" append target moved off progress.md.
    assert (
        "`{{ change.home }}/deferred.md` (create the `## Deferred Followups` heading"
        " if absent)"
    ) in pw
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
    assert (
        "`{{ change.home }}/deferred.md` in reader form (create the file/heading"
        " if absent):"
    ) in rec
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
    assert (
        "**Deferred Followups** list in `{{ change.home }}/deferred.md`,"
        " not a plan's tasks"
    ) in sw
    assert (
        "- `{{ change.home }}/deferred.md` — the **Deferred Followups** list"
        " (the work source)."
    ) in sw
    # Append-only: resolve by checking off, never deleting.
    assert "append-only" in low
    assert "check off" in low
    # The fixed disposition still records the durable opportunistic line in progress.md.
    assert "under `## Completed plans` in\n  `{{ change.home }}/progress.md`" in sw
    # The verify-triage toleration is recorded in deferred.md.
    assert (
        "Record a toleration as a normal `## Deferred Followups` entry in\n"
        "`{{ change.home }}/deferred.md`"
    ) in sw


def test_imp_task_lead_files_ledger_and_forward_notes_to_deferred_md() -> None:
    tl = _imp("task-lead.md")
    # The deferred-finding append target is deferred.md.
    assert (
        "under a\n   `## Deferred Followups` heading in the append-only"
        " `{{ change.home }}/deferred.md`"
    ) in tl
    # Forward notes are appended to deferred.md, never the rewritable progress.md.
    assert "by appending to `{{ change.home }}/deferred.md` — the" in tl
    assert "append-only ledger" in tl
    # The verify-triage toleration is recorded in deferred.md.
    assert (
        "Record a toleration as a normal `## Deferred Followups` entry in\n"
        "`{{ change.home }}/deferred.md`"
    ) in tl
    # progress.md keeps the per-task commit-subject ledger and the Completed-plans collapse.
    assert "keep each task's **commit subject** in `progress.md`" in tl
    assert "it into a single `## Completed plans` line" in tl
    # The forward-note shapes still exist (they just live in deferred.md now).
    assert "- [ ] NEEDS-REPLAN task-lead:" in tl
    assert "- resolved-forward task-lead:" in tl
    # The step-5 lead-in no longer reads as if DEFERRED items go to progress.md.
    assert "(create it if missing) the **transient" in tl
    assert "does **not** live in" in tl


def test_imp_consumers_read_ledger_from_deferred_md() -> None:
    fin = _imp("finalize.md")
    # The closed-set needs-decision read is from deferred.md.
    assert "recorded `— needs-decision: <why>` lines in `{{ change.home }}/deferred.md`, and" in fin
    assert "step recorded in `{{ change.home }}/deferred.md`, and surface ONLY the" in fin
    assert "the `— needs-decision: <why>` lines in `{{ change.home }}/deferred.md`" in fin
    # The gate's line rewrites target deferred.md, not progress.md.
    assert "leave its `deferred.md` line `needs-decision`" in fin
    assert "other than the `deferred.md` line rewrites above" in fin

    gc = _imp("gap-check.md")
    # gap-check reads the needs-decision items (the backstop-suppression source) from
    # deferred.md, alongside progress.md.
    assert (
        "`{{ change.home }}/progress.md`, `{{ change.home }}/deferred.md`,"
        " and the committed code"
    ) in gc

    ir = _imp("integration-review.md")
    assert "`{{ change.home }}/deferred.md` carries any `## Deferred Followups`" in ir


def test_no_prompt_overwrites_deferred_md_wholesale() -> None:
    # deferred.md is append-only: no prompt may instruct a write_file of it,
    # even with the path split across lines.
    for base in (IMP,):
        for p in sorted(base.parent.glob("**/*.md")):
            lines = p.read_text().splitlines()
            for i, line in enumerate(lines):
                if "write_file" in line:
                    window = "\n".join(lines[i : i + 5])
                    assert "deferred.md" not in window, (
                        f"{p.name}: write_file targets the append-only deferred.md"
                    )


def test_imp_forward_notes_survive_progress_md_rewrite() -> None:
    tl = _imp("task-lead.md")
    # The open NEEDS-REPLAN note and resolved-forward note are appended to the
    # append-only deferred.md, which the progress.md collapse never touches.
    assert "by appending to `{{ change.home }}/deferred.md` — the" in tl
    assert "`deferred.md` is\n   never collapsed" in tl
    # progress.md's collapse is explicitly scoped to status only.
    assert "Keep `progress.md` bounded — it holds status only." in tl


def test_imp_completed_plans_and_per_task_status_stay_on_progress_md() -> None:
    tl = _imp("task-lead.md")
    # Transient status references remain on progress.md.
    assert "keep each task's **commit subject** in `progress.md`" in tl
    assert "it into a single `## Completed plans` line" in tl
    # The deferred-sweep records the durable opportunistic line under Completed plans
    # in progress.md (status), not deferred.md.
    assert (
        "under `## Completed plans` in\n"
        "  `{{ change.home }}/progress.md`"
    ) in _imp("deferred-sweep.md")
