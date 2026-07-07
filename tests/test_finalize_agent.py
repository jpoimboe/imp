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
    assert "enqueued into the merge queue" in body
    assert 'no "Merged"' in body
    assert "is now on master" in body
    assert "at the queue head" in body


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
        change_id="x", change_title="t", change_home="/ctl/.imp/changes/x",
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
