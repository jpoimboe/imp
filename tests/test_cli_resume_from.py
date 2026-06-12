import shutil
from pathlib import Path

import pytest

from imp.assets import default_workflow_dir
from imp.cli.main import main
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _git, _init_repo, _seed_control


def _seed_control_default(root: Path) -> None:
    """Like conftest._seed_control, but commits the REAL shipped default
    workflow at imp/workflows/default plus a config defining every tier it
    uses — so Change.create copies the real workflow (with review-panel/recheck, gates,
    a merge phase) into the worktree."""
    _init_repo(root)
    shutil.copytree(default_workflow_dir(), root / "imp" / "workflows" / "default")
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n  low: {anthropic: m}\n  medium: {anthropic: m}\n  high: {anthropic: m}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup (default workflow)")


def test_resume_real_parser_accepts_from_and_force(tmp_path, monkeypatch) -> None:
    # Drive the REAL main() parser. argparse errors raise SystemExit; our code
    # paths return an int. A nonexistent slug on a paused-precondition path
    # returns 2 (LoadError), proving the parser accepted --from/--force without
    # an argparse SystemExit.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main(["resume", "nope", "--from", "code-reviewer", "--force"])
    assert rc == 2          # returned (not raised) => flags parsed fine


def test_no_fork_or_rebase_subcommand() -> None:
    with pytest.raises(SystemExit):
        main(["fork", "feat"])
    with pytest.raises(SystemExit):
        main(["rebase", "feat"])


def test_resume_from_rejects_nonresumable_status(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "completed"
    st.save(ch.state_dir)
    monkeypatch.chdir(tmp_path)
    rc = main(["resume", "feat", "--from", "s"])
    assert rc == 2


def test_resume_from_unknown_target_rc2(tmp_path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.chdir(tmp_path)
    rc = main(["resume", "feat", "--from", "no-such-step"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no-such-step" in err
    assert "valid steps" in err


def test_resume_from_noop_appends_event_without_rewriting_history(
    tmp_path, monkeypatch,
) -> None:
    from imp.engine.loader import load_workflow
    from imp.engine.recovery import apply_rewind, plan_rewind
    from imp.providers.effective import load_effective_config

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    before = _git(tmp_path, "rev-parse", "imp/feat").stdout.strip()

    st = State.load(ch.state_dir)            # stub workflow: cursor at step "s"
    wf = load_workflow(ch.workflow_dir,
                       configured_tiers=load_effective_config(
                           project_root=tmp_path,
                           control_root=tmp_path).config.tier_names())
    plan = plan_rewind(wf, st, st.step, force=False)   # no-op: --from current step
    apply_rewind(plan, wf, st, ch.event_log())
    st.save(ch.state_dir)

    after = _git(tmp_path, "rev-parse", "imp/feat").stdout.strip()
    assert before == after                   # no commit SHAs rewritten
    assert any(e["type"] == "step_rewound" for e in ch.event_log().read())


def test_resume_from_cli_seam_applies_rewind_before_run(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end through _async_cmd_resume (with run_change faked): proves the
    argparse flags -> preflight -> apply_rewind wiring, incl. the workflow_resumed
    BEFORE step_rewound ordering and the persisted cursor/loop-count reset."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult

    _seed_control_default(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "recheck"
    st.status = "paused"
    st.loop_counts = {"task-lead": 2, "recheck": 6}
    st.save(ch.state_dir)

    async def fake_run_change(change, **kwargs):
        return RunResult.COMPLETED

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    args = argparse.Namespace(token="feat", from_target="recheck", force=False)
    rc = anyio.run(m._async_cmd_resume, args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "rewound from" in out
    assert "recheck" in out

    # Event ordering: workflow_resumed precedes step_rewound (no gates_bypassed,
    # this is a backward/self rewind).
    types = [e["type"] for e in ch.event_log().read()]
    assert "workflow_resumed" in types and "step_rewound" in types
    assert types.index("workflow_resumed") < types.index("step_rewound")
    assert "gates_bypassed" not in types

    # Persisted cursor + downstream-only loop-count reset.
    final = State.load(ch.state_dir)
    assert final.step == "recheck"
    assert final.loop_counts == {"task-lead": 2}     # recheck reset, upstream kept


def test_resume_from_cli_seam_forced_forward_lands_as_is(tmp_path, monkeypatch) -> None:
    """The --force forward path through the real CLI seam: --from merge --force
    from a paused recheck emits gates_bypassed (after workflow_resumed),
    sets ready_to_merge, and lands the cursor at the merge phase entry
    (base-fresh-check) so the engine-native merge step runs (B3 cutover)."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult

    _seed_control_default(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "recheck"
    st.status = "paused"
    st.save(ch.state_dir)

    async def fake_run_change(change, **kwargs):
        return RunResult.COMPLETED

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    args = argparse.Namespace(token="feat", from_target="merge", force=True)
    rc = anyio.run(m._async_cmd_resume, args)
    assert rc == 0

    # Ordering: workflow_resumed < gates_bypassed < step_rewound; gates named.
    events = list(ch.event_log().read())
    types = [e["type"] for e in events]
    assert types.index("workflow_resumed") < types.index("gates_bypassed")
    assert types.index("gates_bypassed") < types.index("step_rewound")
    bypassed = next(e for e in events if e["type"] == "gates_bypassed")
    assert bypassed["payload"]["gates"] == [
        "recheck", "integrate-merge", "integrate-tests",
        "integration-review", "final-review",
    ]

    # Persisted: cursor lands at the merge phase entry + ready_to_merge set
    # (land as-is). The merge phase's engine-native steps run the merge (B3).
    final = State.load(ch.state_dir)
    assert final.step == "base-fresh-check"
    assert final.vars["ready_to_merge"] is True


def test_resume_rechecks_abandoned_status_under_lock(tmp_path, monkeypatch) -> None:
    """Guards the abandon/resume TOCTOU: the precondition read is unlocked, so a
    concurrent `imp abandon` can flip status to 'abandoned' after it passes.
    Resume re-reads status under the lock and refuses (rc 2) without running.
    Modelled by flipping the on-disk status between the two State.load calls
    (early check = call 1; under-lock recheck = call 2)."""
    import argparse

    import anyio

    import imp.cli.main as m

    _seed_control_default(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "recheck"
    st.status = "paused"
    st.save(ch.state_dir)

    real_load = State.load.__func__
    calls = {"n": 0}

    def fake_load(cls, state_dir):
        calls["n"] += 1
        s = real_load(cls, state_dir)
        if calls["n"] >= 2:          # the under-lock recheck sees abandon's write
            s.status = "abandoned"
        return s

    ran = {"called": False}

    async def fake_run_change(change, **kwargs):
        ran["called"] = True
        from imp.engine.executor import RunResult
        return RunResult.COMPLETED

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)
    monkeypatch.setattr(m.State, "load", classmethod(fake_load))

    args = argparse.Namespace(token="feat", from_target=None, force=False)
    rc = anyio.run(m._async_cmd_resume, args)
    assert rc == 2
    assert ran["called"] is False    # never ran a Change abandon finalized


def test_rewind_note_line_states_from_and_to() -> None:
    from imp.cli.main import _rewind_note_line
    assert _rewind_note_line("plan", "spec", color=False) == \
        "rewound from plan to spec"
    # A None cursor (rewind to the very start) reads as 'start'.
    assert _rewind_note_line(None, "spec", color=False) == \
        "rewound from start to spec"


def test_async_cmd_resume_installs_benign_loop_handler(tmp_path, monkeypatch) -> None:
    import argparse
    import contextlib

    import anyio

    import imp.cli.main as m
    from imp.engine.change import Change
    from imp.engine.state import State

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    entered = {"n": 0}

    @contextlib.contextmanager
    def fake_handler(coord):
        entered["n"] += 1
        yield

    async def fake_session(change, *, tg, **kwargs):
        tg.cancel_scope.cancel()
        return 0

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_run_change_session", fake_session)
    monkeypatch.setattr(m, "_benign_interrupt_loop_handler", fake_handler)

    args = argparse.Namespace(
        token="feat", from_target=None, force=False, tui=False,
        non_interactive=True, message="go")
    rc = anyio.run(m._async_cmd_resume, args)
    assert rc == 0
    assert entered["n"] == 1
