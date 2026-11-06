from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog

WF = Path("src/imp/assets/workflows/default")


def test_reconciler_declares_stuck_envelope() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "reconciler.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "deviation" not in fields             # the deviation escalation is gone
    assert fields["stuck"].type == "bool"
    assert set(fields["status"].values) == {"Success", "Error"}
    assert "summary" in fields


def test_reconciler_body_is_human_free_and_defines_contexts() -> None:
    body = (WF / "agents" / "reconciler.md").resolve().read_text()
    assert "wait_for_user" not in body           # the held region is human-free
    assert "breakage" not in body.lower()
    assert "stuck: true" in body                 # escalate criterion
    assert "stuck: false" in body                # reconcile criterion
    assert "conflicted rebase" in body.lower()   # entry context 1
    assert "integration-verify" in body          # both in merged context 2
    assert "integration-review" in body          # both in merged context 2
    assert "integration-notes.md" not in body    # no bespoke integration artifact


def test_reconciler_is_finding_driven_not_raw_test_output() -> None:
    body = (WF / "agents" / "reconciler.md").read_text()
    low = body.lower()
    # Context 2 stays finding-driven: an integration-verify failure arrives only as a finding.
    assert "three entry contexts" in low
    assert "two entry contexts" not in low
    assert "never raw test output" in low


_MEMORY_CONTEXT = """\
3. **Resumed with a memory-validation failure** — feedback *"The imp/memory/ tree is
   invalid against the rebased workflows. Fix the memory file paths:"*, no rebase in
   progress. The validator's output names the invalid paths. Move, rename, or remove those
   memory files to match the rebased workflows, in a NORMAL fix commit on top (`git add` +
   `git commit -F`). Do not start or continue a rebase.
"""


def test_reconciler_renders_the_memory_validation_context(tmp_path: Path) -> None:
    wf = bundled_workflow_dir("default")
    out = render_prompt(
        load_workflow(wf).agents["reconciler"].prompt_template_source,
        build_context(
            quest_title="t", event_log=JsonlEventLog(tmp_path / "events.jsonl"),
            quest_id="abc", quest_home=".imp/quests/abc",
            vars={"integration_fork": "FORK"},
        ),
        workflow_dir=wf,
    )
    assert "## Three entry contexts — detect which" in out
    assert _MEMORY_CONTEXT in out
    assert "Two entry contexts" not in out
