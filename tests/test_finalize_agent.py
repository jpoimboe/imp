from pathlib import Path

import pytest

WF = Path("src/imp/assets/workflows/default")


def test_finalize_no_longer_reviews_integration_resolution() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "integration-notes.md" not in body
    assert "Review the merge resolution" not in body


def test_finalize_still_decides_merge_or_rework() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "finalize.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert set(fields["decision"].values) == {"merge", "rework", "pause"}


def test_finalize_forbids_premature_merge_claim() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    # The "do not assert the merge as already done" guard is preserved verbatim.
    assert 'no "Merged"' in body
    assert "is now on master" in body
    # Its justification is corrected: the mechanical merge runs right after finalize
    # returns, not "later, at the queue head".
    assert "runs immediately after you return" in body
    assert "at the queue head" not in body
    # The stale enqueue framing at the merge disposition is gone. (The source
    # wraps "enqueues the change in the\n  merge queue", so assert the contiguous
    # line-start substring that genuinely exists pre-fix, not the wrapped whole.)
    assert "enqueued into the merge queue" not in body
    assert "enqueues the change in the" not in body


def test_finalize_pause_route_drops_human_availability_reasoning() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "wait_for_user" not in body
    assert "and none is available" not in body
    assert 'decision="pause"' in body
    assert "non-converging verify-triage" in body


def test_finalize_enumerates_the_two_gate_toleration_sources() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "converge-verify" not in body
    # The audit-field read, the already-tracked check and the merge-gate walk each name both gates.
    flat = " ".join(body.split())
    assert flat.count("`verify` / `integration-verify`") == 3


def _render_finalize(tmp_path: Path, *, auto_merge: bool, autonomous: bool) -> str:
    from imp.agents.prompts import build_context, render_prompt
    from imp.assets import bundled_workflow_dir
    from imp.engine.state import JsonlEventLog

    wf_dir = bundled_workflow_dir("default")
    body = (wf_dir / "agents" / "finalize.md").read_text()
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(
        quest_id="x", quest_title="t", quest_home="/ctl/.imp/quests/x",
        event_log=log, vars={"auto_merge": auto_merge}, autonomous=autonomous,
    )
    return render_prompt(body, ctx, workflow_dir=wf_dir)


_AUTO_MERGE_MARKER = "merges clean quests without asking"
_INTERACTIVE_MARKER = "This is the final **merge gate**"
_AUTONOMOUS_MARKER = "Autonomous run — no human at the gate."


def test_auto_merge_branch_renders_with_interactive_gate_beneath(tmp_path: Path) -> None:
    # auto_merge true, non-autonomous: BOTH the auto-merge instruction AND the
    # full interactive gate text render (the block is additive, not an elif).
    out = _render_finalize(tmp_path, auto_merge=True, autonomous=False)
    assert _AUTO_MERGE_MARKER in out
    assert _INTERACTIVE_MARKER in out


def test_auto_merge_branch_omitted_when_false(tmp_path: Path) -> None:
    # auto_merge false: the instruction is gone, the interactive gate remains.
    out = _render_finalize(tmp_path, auto_merge=False, autonomous=False)
    assert _AUTO_MERGE_MARKER not in out
    assert _INTERACTIVE_MARKER in out


def test_auto_merge_branch_absent_in_autonomous_arm(tmp_path: Path) -> None:
    # Autonomous arm is unchanged: the auto-merge block lives in the else arm,
    # so it never renders under autonomy even with auto_merge true.
    out = _render_finalize(tmp_path, auto_merge=True, autonomous=True)
    assert _AUTO_MERGE_MARKER not in out
    assert _INTERACTIVE_MARKER not in out
    assert _AUTONOMOUS_MARKER in out


def test_finalize_frames_change_as_already_at_head() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    # None of the stale "entering / enqueuing the queue" phrasings remain anywhere.
    assert "enters the merge queue" not in body
    assert "(enqueue)" not in body
    assert "enqueues the change in the" not in body
    assert "enqueued into the merge queue" not in body
    assert "Integration review happens later, at the head of the queue" not in body
    # The change is described as already at the head, with the at-head checks already done.
    assert "already at the head" in body
    assert "already re-checked whatever integration changed" in body


def test_finalize_description_frames_head_of_queue() -> None:
    from imp.engine.loader import parse_agent_file

    desc = parse_agent_file((WF / "agents" / "finalize.md").resolve()).description
    assert "head of the merge queue" in desc
    assert "Integration review happens later" not in desc


def _flat(text: str) -> str:
    return " ".join(text.split())


_DECIDED_NEVER_GATES = (
    "A `— decided:` line is **never** in `open_items`: it records a call an agent already "
    "made, born ticked, so it does not count toward the auto-merge empty-set condition, is "
    "never walked as a needs-decision item, and is never seeded to the backlog — the "
    "summary lists it instead.")


@pytest.mark.parametrize("autonomous", [False, True])
def test_decided_lines_never_gate_or_seed_in_either_render(
        tmp_path: Path, autonomous: bool) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=True, autonomous=autonomous))
    assert _DECIDED_NEVER_GATES in out


def test_autonomous_branch_never_seeds_a_decided_line(tmp_path: Path) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=True, autonomous=True))
    assert ("Never seed a `— decided:` line — it is a call already made, not an open "
            "question; leave it untouched.") in out
    interactive = _flat(_render_finalize(tmp_path, auto_merge=True, autonomous=False))
    assert "Never seed a `— decided:` line" not in interactive


def test_interactive_gate_paragraph_absent_from_autonomous_render(tmp_path: Path) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=True, autonomous=True))
    assert "A `— decided:` line is **not** walked" not in out


def test_auto_merge_empty_set_ignores_decided_lines(tmp_path: Path) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=True, autonomous=False))
    assert ("is empty (`— decided:` lines never count toward it) AND there is no "
            "unrecoverable problem") in out
    assert "and is never seeded on **defer**" in out


def test_auto_merge_comments_point_at_the_summary(tmp_path: Path) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=True, autonomous=False))
    assert ("state in `comments` that you merged automatically because nothing needed a "
            "decision, and that any calls made along the way are listed in the summary.") in out
    assert "because there were no open items" not in out


def test_gate_fix_strikes_a_decided_line(tmp_path: Path) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=False, autonomous=False))
    assert ("A `— decided:` line is **not** walked — the summary you open with already "
            "lists it") in out
    assert ("When the human asks to change one, map it to **fix**: collect the change into "
            "the rework brief and rewrite the line to "
            "`- [x] DEFERRED <src>: <desc> [scope: …] — overridden-at-gate`, so a re-run "
            "summary stops listing it. That is the only rewrite a `— decided:` line ever "
            "gets.") in out


def test_absent_summary_fallback_lists_decided_lines(tmp_path: Path) -> None:
    out = _flat(_render_finalize(tmp_path, auto_merge=False, autonomous=False))
    assert ("when that artifact is absent, a concise summary you compose from the audit "
            "record, followed — when `/ctl/.imp/quests/x/deferred.md` has any `— decided:` "
            "lines — by `Decided without asking you:` and one "
            "`- <what was decided> — <why>` bullet per line), then the verification state "
            "in plain terms.") in out
