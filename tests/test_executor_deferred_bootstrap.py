"""run_quest bootstraps a deferred (never-seeded) backlog draft on its first
resume: it stamps the cursor + vars from the freshly-loaded workflow via the
shared seed_workflow_start helper, without a spurious vars_backfilled event; a
genuinely completed quest (step None, has_started True) still completes untouched."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.engine.executor import RunResult, run_quest
from imp.engine.state import JsonlEventLog, State
from tests.test_executor import _make_change, _register_invocation_capturing

_FAKE_CONFIG_WITH_VAR = (
    "providers: [fake]\n"
    "tiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    "vars:\n  n: 42\n"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_var_workflow(root: Path) -> object:
    """A one-agent-step workflow: var `n` (default 7, config-overridden to 42),
    phase label `P`, body echoing `vars.n`. _make_change seeds an EAGER State that
    the caller overwrites with the unseeded deferred-draft shape before running."""
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "n={{ vars.n }}\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "vars:\n  n: {type: int, default: 7}\n"
        "phases:\n  - name: only\n    label: P\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG_WITH_VAR)
    return _make_change(root)


def _task_text(inv: object) -> str:
    """The rendered agent body (first user message) — where `vars.n` interpolates."""
    return inv.initial_messages[0].content[0].text


@pytest.mark.anyio
async def test_first_resume_bootstraps_deferred_draft(tmp_path: Path) -> None:
    quest = _seed_var_workflow(tmp_path)
    # Overwrite _make_change's eager State with the shape create_draft(defer_seed=
    # True) writes: null cursor, never started, no vars.
    State(id=quest.id, step=None, step_attempt=0, status="new").save(quest.state_dir)

    snapshots: list = []

    def _snap(_e: object) -> None:
        st = State.load(quest.state_dir)
        if st.step is not None:
            snapshots.append(
                (st.step, st.step_label, st.phase_label, st.vars.get("n")))

    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_quest(quest, on_event=_snap)

    assert result is RunResult.COMPLETED
    # Cursor was seeded -> the first step actually dispatched. An un-bootstrapped
    # draft keeps step=None and _complete's immediately, invoking no provider.
    assert sink, "provider was never invoked -> cursor was not seeded"
    # The seeded cursor + vars captured at first dispatch match an eagerly-seeded
    # draft: step w, no step label (unlabeled step -> honest None), phase label P,
    # and the var override 42 (not the bare default 7).
    assert snapshots[0] == ("w", None, "P", 42)
    # The override reached the agent through apply_var_overrides, not backfill.
    assert "n=42" in _task_text(sink[0])
    # The initial seed must not masquerade as a late backfill.
    types = [e["type"] for e in JsonlEventLog(quest.events_path).read()]
    assert "vars_backfilled" not in types


@pytest.mark.anyio
async def test_completed_quest_still_completes_without_bootstrap(tmp_path: Path) -> None:
    quest = _seed_var_workflow(tmp_path)
    # A genuinely completed quest: null cursor but has_started True. The sentinel
    # must skip the bootstrap and _complete must run — no re-seed, no dispatch.
    st = State(id=quest.id, step=None, step_attempt=0, status="new")
    st.has_started = True
    st.save(quest.state_dir)

    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_quest(quest)

    assert result is RunResult.COMPLETED
    assert not sink, "a completed quest must not re-seed and dispatch a step"


def _seed_var_workflow_with_git(root: Path) -> object:
    """Like _seed_var_workflow but also initialises a minimal git repo so
    Quest.create_draft can call head_of() when exercising the eager path."""

    def _g(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)

    _g("init", "-q", "-b", "main")
    _g("config", "user.email", "t@t")
    _g("config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _g("add", "-A")
    _g("commit", "-qm", "seed")
    return _seed_var_workflow(root)


@pytest.mark.anyio
async def test_deferred_and_eager_creation_yield_identical_seeded_state(
    tmp_path: Path,
) -> None:
    """Acceptance criterion for quest vava71: a deferred-then-resumed draft and
    an eagerly-created quest reach byte-identical seeded State fields."""
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    quest_deferred = _seed_var_workflow_with_git(tmp_path)
    State(id=quest_deferred.id, step=None, step_attempt=0, status="new").save(
        quest_deferred.state_dir,
    )

    first_seeded: list[State] = []

    def _snap(_e: object) -> None:
        if first_seeded:
            return
        st = State.load(quest_deferred.state_dir)
        if st.step is not None:
            first_seeded.append(st)

    await run_quest(quest_deferred, on_event=_snap)
    assert first_seeded, "deferred path never bootstrapped"

    from imp.engine.quest import Quest

    quest_eager = Quest.create_draft(
        tmp_path,
        title="eager",
        quest_id="eager-q",
        base_branch="main",
        defer_seed=False,
    )
    state_eager = State.load(quest_eager.state_dir)

    st_d = first_seeded[0]
    deferred_tuple = (st_d.step, st_d.step_label, st_d.phase_label, st_d.vars.get("n"))
    eager_tuple = (
        state_eager.step,
        state_eager.step_label,
        state_eager.phase_label,
        state_eager.vars.get("n"),
    )
    assert deferred_tuple == eager_tuple
    assert st_d.vars == state_eager.vars
