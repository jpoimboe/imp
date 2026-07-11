from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


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


def test_finalize_enumerates_all_four_toleration_sources() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "converge-verify" in body and "integration-verify" in body
    # Both enumeration spots (the audit-field read and the merge-gate walk) name the new gates.
    assert body.count("converge-verify") >= 2
    assert body.count("integration-verify") >= 2


def _render_finalize(tmp_path: Path, *, auto_merge: bool, autonomous: bool) -> str:
    from imp.agents.prompts import build_context, render_prompt
    from imp.assets import bundled_workflow_dir
    from imp.engine.state import JsonlEventLog

    wf_dir = bundled_workflow_dir("imp")
    body = (wf_dir / "agents" / "finalize.md").read_text()
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(
        quest_id="x", quest_title="t", quest_home="/ctl/.imp/quests/x",
        event_log=log, vars={"auto_merge": auto_merge}, autonomous=autonomous,
    )
    return render_prompt(body, ctx, workflow_dir=wf_dir)


_AUTO_MERGE_MARKER = "merges clean changes without asking"
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
    # The change is described as already at the head, with integration-review already done.
    assert "already at the head" in body
    assert "already re-reviewed it at the merge head" in body


def test_finalize_description_frames_head_of_queue() -> None:
    from imp.engine.loader import parse_agent_file

    desc = parse_agent_file((WF / "agents" / "finalize.md").resolve()).description
    assert "head of the merge queue" in desc
    assert "Integration review happens later" not in desc
