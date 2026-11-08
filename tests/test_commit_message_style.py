from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path("src/imp/assets")
PARTIAL = ASSETS / "partials" / "commit-style.md"
DEFAULT_AGENTS = ASSETS / "workflows" / "default" / "agents"
MINIMAL_AGENTS = ASSETS / "workflows" / "minimal" / "agents"
MEMORY_AGENTS = ASSETS / "workflows" / "memory" / "agents"
INCLUDE = '{% include "partials/commit-style.md" %}'

# A bare `git commit` span is exempt only when a negation sits within a few words.
ALLOWED_TAILS = (" -F", " --fixup=", " --allow-empty -F")
PROHIBITION = re.compile(r"\b(?:never|not|don't)\b", re.IGNORECASE)
PROHIBITION_REACH = 30


def _bare_commit_lines(text: str) -> list[int]:
    offending: list[int] = []
    for match in re.finditer(r"git commit(?![-\w])", text):
        tail = text[match.end():]
        if tail.startswith(ALLOWED_TAILS):
            continue
        start = match.start()
        end = match.end()
        if (start > 0 and text[start - 1] == "`" and
                end < len(text) and text[end] == "`"):
            window = text[max(0, start - PROHIBITION_REACH):start].replace("\n", " ")
            if PROHIBITION.search(window):
                continue
        offending.append(text[:start].count("\n") + 1)
    return offending


def test_commit_style_partial_states_the_canonical_rule() -> None:
    text = PARTIAL.read_text()
    flat = " ".join(text.split())
    assert text.startswith("## Commit messages\n")
    # The message-file mandate, not an example of one.
    assert '`git commit -F "${TMPDIR:-/tmp}/msg"` — never `-m`' in flat
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
    assert 'git commit -F "${TMPDIR:-/tmp}/msg"' in text
    assert "git commit -m" not in text


def test_coder_documents_the_amend_reword_mode() -> None:
    flat = " ".join((DEFAULT_AGENTS / "coder.md").read_text().split())
    assert "**REWORDING**" in flat
    # The message file's first line is the reword directive git's autosquash reads.
    assert "first line is `amend! ` plus the target commit's subject" in flat
    assert 'git commit --allow-empty -F "${TMPDIR:-/tmp}/msg"' in flat
    assert "Every `fixup!` and `amend!` commit is transient" in flat


def test_sweep_and_plan_skeleton_commit_with_a_message_file() -> None:
    sweep = (DEFAULT_AGENTS / "deferred-sweep.md").read_text()
    plan_writer = (DEFAULT_AGENTS / "plan-writer.md").read_text()
    # The sweep does not include the partial, so each dispatch string must itself
    # point at the rule — the same clause the task-lead bullet carries.
    assert sweep.count(
        "`git add <files> && git commit -F` with a message in the house style") == 1
    # A lower bound so a future second mention is not a failure.
    assert sweep.count("git commit -F") >= 1        # the standalone item commit
    assert "git commit -m" not in sweep
    # The skeleton stops modelling the old shape in every plan written from it.
    assert 'git commit -F "${TMPDIR:-/tmp}/msg"' in plan_writer
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
    assert 'git add imp/memory/<path>… && git commit -F "${TMPDIR:-/tmp}/msg"' in text
    assert "git commit -m" not in text
    assert "`memory: <imperative summary, no trailing period>`" in flat
    assert "one paragraph saying which standing instruction changed and why" in flat


def test_summarizer_pins_the_leading_paragraph_contract() -> None:
    flat = " ".join((DEFAULT_AGENTS / "summarizer.md").read_text().split())
    assert "Lead the file with this paragraph — no heading above it" in flat
    assert "hard-wrapped at 75 columns" in flat
    assert "Everything before the first blank line becomes the merge commit's body." in flat
    # The existing headline framing survives the addition.
    assert "one tight paragraph: what this quest accomplishes and why" in flat


def _asset_texts() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in sorted(ASSETS.rglob("*"))
            if p.is_file() and p.suffix in {".md", ".sh", ".yaml"}]


def test_no_asset_teaches_a_dash_m_commit() -> None:
    offenders = [str(p) for p, text in _asset_texts() if "git commit -m" in text]
    assert offenders == [], f"-m commit survives in: {offenders}"


def test_no_asset_writes_to_an_unguarded_tmpdir_path() -> None:
    """TMPDIR is unset on a sandbox-off run, so a bare-variable path resolves to /<name>."""
    offenders = [str(p) for p, text in _asset_texts() if "$TMPDIR/" in text]
    assert offenders == [], f"unguarded $TMPDIR path in: {offenders}"


def test_every_asset_git_commit_invocation_names_a_message_file_or_fixup() -> None:
    offenders: list[str] = []
    for path, text in _asset_texts():
        for line in _bare_commit_lines(text):
            offenders.append(f"{path}:{line}")
    assert offenders == [], f"bare `git commit` at: {offenders}"


def test_bare_commit_lines_discrimination() -> None:
    cases: list[tuple[str, bool]] = [
        # flagged — teaching forms
        ("(`git add` + `git commit`)", True),
        ("commit with `git commit` now", True),
        # exempt — ALLOWED_TAILS
        ('git commit -F "${TMPDIR:-/tmp}/msg"', False),
        ("git commit --fixup=HEAD", False),
        ('git commit --allow-empty -F "${TMPDIR:-/tmp}/msg"', False),
        # exempt — prohibition with negation within reach
        ("Do not `git add`/`git commit`.", False),
        ("never runs `git add`/`git commit` on project", False),
        ("never run `git commit` yourself", False),
        ("you must never\nrun `git commit` yourself", False),
    ]
    for snippet, should_flag in cases:
        lines = _bare_commit_lines(snippet)
        flagged = len(lines) > 0
        assert flagged == should_flag, (
            f"snippet {snippet!r}: expected flagged={should_flag}, got {lines}"
        )
