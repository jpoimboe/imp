from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow, parse_agent_file

AGENTS = Path("src/imp/assets/workflows/default/agents")
SWEEP = AGENTS / "deferred-sweep.md"


def test_deferred_sweep_parses_with_expected_contract() -> None:
    agent = parse_agent_file(SWEEP)
    assert agent.name == "deferred-sweep"
    assert set(agent.subagents) == {"coder", "code-reviewer"}
    fields = agent.output_schema.field_names()
    assert "status" in fields and "summary" in fields


def test_deferred_sweep_resolves_to_dispatcher_tools() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    agent = wf.agents["deferred-sweep"]
    for tool in ("read_file", "write_file", "edit_file", "bash", "sub_agent"):
        assert tool in agent.tools, f"missing tool {tool}"
    assert "complete_step" not in agent.tools


def test_deferred_sweep_loads_as_part_of_the_workflow() -> None:
    wf = load_workflow(bundled_workflow_dir("default"),
                       configured_tiers=frozenset({"low", "medium", "high"}))
    assert "deferred-sweep" in wf.agents


def test_deferred_sweep_checks_off_and_records_fixes_durably() -> None:
    low = SWEEP.read_text().lower()
    # Three dispositions of the risk-gated model survive.
    assert "record it `fixed`" in low and "needs-decision" in low and "dismissed" in low
    # Risk, not scope, is the gate.
    assert "low-risk" in low and "high-risk" in low
    # The pinned input line the sweep parses.
    assert "- [ ] deferred" in low
    # Reviewer discipline preserved.
    assert "no code-reviewer" in low or "without a code-reviewer" in low
    pos = low.find("non-trivial")
    assert pos >= 0 and "code-reviewer" in low[pos : pos + 250]
    assert "one clean commit" in low
    # A FIXED item is recorded durably under ## Completed plans, then the ledger
    # line is CHECKED OFF (append-only: flipped to - [x] with a terminal marker,
    # never deleted).
    assert "## completed plans" in low
    assert 'fixed in "' in low                       # durable line carries the subject
    assert "opportunistic (" in low                  # durable line prefix that finalize parses
    assert "check off" in low and "append-only" in low
    assert "- [x] deferred" in low                   # the checked-off terminal shape
    assert "prune" not in low                        # the old delete model must not return
    # A DISMISSED item is checked off with NO durable record; needs-decision STAYS open.
    assert "no durable record" in low
    assert "stays open" in low
    # Fail-safe: malformed lines untouched; polish never marks the run degraded.
    assert "malformed" in low
    assert "degraded" in low
    # The terminal gate markers stay inert.
    assert "seeded <quest" in low and "ignored-at-gate" in low
    assert "needs-decision:" in low                  # parseable format that finalize reads
    assert "never grow the change" not in low
    assert "auto-route" not in low


def test_deferred_sweep_calibration_clauses() -> None:
    low = SWEEP.read_text().lower()
    # needs-decision must not absorb low-stakes "is it worth doing?" calls.
    nd = low.find("needs-decision** — the item")
    assert nd >= 0
    nd_block = low[nd : nd + 800]
    assert "worth doing" in nd_block
    assert "fix it or dismiss it" in nd_block
    # fixed bucket recognizes the fail-fast guard in definitional framing.
    fx = low.find("fixed** — the improvement")
    assert fx >= 0
    fixed_block = low[fx : fx + 900]
    assert "fail-fast" in fixed_block
    assert "silent" in fixed_block and "loud" in fixed_block
    assert "reachable today" in fixed_block
    assert "dismissable" in fixed_block
    # The spec-prohibited numeric fence must not appear (definitional framing only).
    assert "four-test" not in fixed_block


_SWEEP_BAN = ("- You **never run the gate, the full suite, lint, or typecheck yourself — "
              "not even to confirm a fix**: coders run focused tests only, and "
              "`integration-verify` is the quest's one slow run.")


def test_deferred_sweep_does_no_gate_triage() -> None:
    body = SWEEP.read_text()
    flat = " ".join(body.split())
    # No gate resumes the sweep any more: the triage section and its flag are gone.
    assert "## Verify triage" not in body
    assert "converge-verify" not in body and "converge_verify" not in body
    assert "injected feedback" not in body
    # The sweep runs after the last `verify`, so the merge-queue slow run backstops it.
    assert "The `integration-verify` gate is the backstop" in flat
    assert "The `verify` gate is the backstop" not in flat


def test_deferred_sweep_dispatch_ends_with_the_self_run_ban() -> None:
    dispatch = SWEEP.read_text().split("## Dispatch", 1)[1].split("\n## ", 1)[0]
    bullets = [line for line in dispatch.splitlines() if line.startswith("- ")]
    assert bullets[-1] == _SWEEP_BAN


def test_deferred_sweep_treats_tracked_by_as_terminal() -> None:
    body = SWEEP.read_text()
    low = body.lower()
    # The gate's third terminal marker joins seeded / ignored-at-gate.
    assert "three further terminal markers" in low
    assert "— tracked-by <quest-id>" in body
    # It is listed in the drain-only-untagged invariant's terminal-tag set.
    assert "`— ignored-at-gate` / `— tracked-by`)" in body
    # Like the sibling gate markers, it is inert to the sweep.
    assert "inert" in low
