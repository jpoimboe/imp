from __future__ import annotations

from pathlib import Path

ASSETS = Path("src/imp/assets")
PARTIAL = ASSETS / "partials" / "commit-style.md"
DEFAULT_AGENTS = ASSETS / "workflows" / "default" / "agents"
MINIMAL_AGENTS = ASSETS / "workflows" / "minimal" / "agents"
MEMORY_AGENTS = ASSETS / "workflows" / "memory" / "agents"
INCLUDE = '{% include "partials/commit-style.md" %}'


def test_commit_style_partial_states_the_canonical_rule() -> None:
    text = PARTIAL.read_text()
    flat = " ".join(text.split())
    assert text.startswith("## Commit messages\n")
    # The message-file mandate, not an example of one.
    assert '`git commit -F "$TMPDIR/msg"` — never `-m`' in flat
    assert "**Subject:** `component: imperative summary` — no trailing period" in flat
    assert "under 100 characters" in flat
    # The corollary that kills the feat:/chore: reflex.
    assert (
        "names the **part of the codebase that changed**, not the kind of change"
    ) in flat
    assert "`git log --oneline -30 -- <the paths you touched>`" in flat
    assert "**Body:** wrapped at 75 columns, three movements, no headings:" in flat
    assert "1. **Context** — present tense, describing the deficient status quo." in flat
    assert "2. **Consequence** — what actually goes wrong" in flat
    assert "3. **The fix** — imperative mood." in flat
    assert "A body is required." in flat
    assert "**Voice:** impersonal" in flat
    assert "Never reference a commit from this same quest" in flat
    assert "git commit -m" not in text


def test_coder_includes_the_commit_style_partial() -> None:
    text = (DEFAULT_AGENTS / "coder.md").read_text()
    assert INCLUDE in text
    assert 'git commit -F "$TMPDIR/msg"' in text
    assert "git commit -m" not in text


def test_coder_documents_the_amend_reword_mode() -> None:
    flat = " ".join((DEFAULT_AGENTS / "coder.md").read_text().split())
    assert "**REWORDING**" in flat
    # The message file's first line is the reword directive git's autosquash reads.
    assert "first line is `amend! ` plus the target commit's subject" in flat
    assert 'git commit --allow-empty -F "$TMPDIR/msg"' in flat
    assert "Every `fixup!` and `amend!` commit is transient" in flat


def test_sweep_and_plan_skeleton_commit_with_a_message_file() -> None:
    sweep = (DEFAULT_AGENTS / "deferred-sweep.md").read_text()
    plan_writer = (DEFAULT_AGENTS / "plan-writer.md").read_text()
    # The sweep does not include the partial, so each dispatch string must itself
    # point at the rule — the same clause the task-lead bullet carries.
    assert sweep.count(
        "`git add <files> && git commit -F` with a message in the house style") == 2
    # Both sites converted; a lower bound so a future third mention is not a failure.
    assert sweep.count("git commit -F") >= 2        # the item commit and the sweep fix
    assert "git commit -m" not in sweep
    # The skeleton stops modelling the old shape in every plan written from it.
    assert 'git commit -F "$TMPDIR/msg"' in plan_writer
    assert "git commit -m" not in plan_writer


def test_task_lead_dispatches_message_file_commits_and_amend_rewords() -> None:
    flat = " ".join((DEFAULT_AGENTS / "task-lead.md").read_text().split())
    assert "`git add <files>` then `git commit -F` with a message in the house style" in flat
    assert "git commit -m" not in flat
    # The staleness escape hatch is dispatched, not left to the coder to notice.
    assert "**Message gone stale:**" in flat
    assert "record an `amend!` reword" in flat
    # The trailer contract is untouched.
    assert "No `Task:` trailer, no `Quest:` trailer" in flat


def test_reconciler_includes_the_partial_and_names_a_message_file() -> None:
    text = (DEFAULT_AGENTS / "reconciler.md").read_text()
    assert INCLUDE in text
    assert "(`git add` + `git commit -F`)" in text


def test_minimal_commit_authors_include_the_partial_and_name_a_message_file() -> None:
    implementer = (MINIMAL_AGENTS / "implementer.md").read_text()
    finalize = (MINIMAL_AGENTS / "finalize.md").read_text()
    for text in (implementer, finalize):
        assert INCLUDE in text
        assert "git add` + `git commit -F`" in text
        assert "git commit -m" not in text


def test_memory_editor_keeps_its_own_rule_without_the_partial() -> None:
    text = (MEMORY_AGENTS / "memory-editor.md").read_text()
    flat = " ".join(text.split())
    # Fixed component, one file: the derivation rules would be dead weight here.
    assert INCLUDE not in text
    assert 'git add imp/memory/<path>… && git commit -F "$TMPDIR/msg"' in text
    assert "git commit -m" not in text
    assert "`memory: <imperative summary, no trailing period>`" in flat
    assert "one paragraph saying which standing instruction changed and why" in flat
