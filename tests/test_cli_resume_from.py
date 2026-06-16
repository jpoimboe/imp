import shutil
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.cli.main import main
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _git, _init_repo, _seed_control


def _seed_control_default(root: Path) -> None:
    """Like conftest._seed_control, but commits the REAL shipped default
    workflow at imp/workflows/imp plus a config defining every tier it
    uses — so Change.create copies the real workflow (with the per-plan review +
    deferred-sweep, gates, a merge phase) into the worktree."""
    _init_repo(root)
    shutil.copytree(bundled_workflow_dir("imp"), root / "imp" / "workflows" / "imp")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n  low: {anthropic: m}\n  medium: {anthropic: m}\n  high: {anthropic: m}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup (default workflow)")


def test_resume_real_parser_accepts_from(tmp_path, monkeypatch) -> None:
    # Drive the REAL main() parser. A nonexistent slug on a paused-precondition
    # path returns 2 (LoadError), proving the parser accepted --from without an
    # argparse SystemExit.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main(["resume", "nope", "--from", "code-reviewer"])
    assert rc == 2          # returned (not raised) => --from parsed fine


def test_resume_parser_rejects_removed_force_flag(tmp_path, monkeypatch) -> None:
    # --force was removed; argparse rejects the unknown option with SystemExit.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["resume", "nope", "--from", "code-reviewer", "--force"])


def test_no_fork_or_rebase_subcommand(capsys) -> None:
    # `fork`/`rebase` are not real subcommands. Since imp-entrypoint-help-ux,
    # an unknown command returns rc 2 with a friendly message (no SystemExit).
    assert main(["fork", "feat"]) == 2
    assert "unknown command 'fork'" in capsys.readouterr().err
    assert main(["rebase", "feat"]) == 2
    assert "unknown command 'rebase'" in capsys.readouterr().err


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
    plan = plan_rewind(wf, st, st.step)   # no-op: --from current step
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
    st.step = "review"
    st.status = "paused"
    st.loop_counts = {"task-lead": 2, "review": 6}
    st.save(ch.state_dir)

    async def fake_run_change(change, **kwargs):
        return RunResult.COMPLETED

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    args = argparse.Namespace(token="feat", from_target="review")
    rc = anyio.run(m._async_cmd_resume, args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "rewound from" in out
    assert "review" in out

    # Event ordering: workflow_resumed precedes step_rewound (no gates_bypassed,
    # this is a backward/self rewind).
    types = [e["type"] for e in ch.event_log().read()]
    assert "workflow_resumed" in types and "step_rewound" in types
    assert types.index("workflow_resumed") < types.index("step_rewound")
    assert "gates_bypassed" not in types

    # Persisted cursor + downstream-only loop-count reset.
    final = State.load(ch.state_dir)
    assert final.step == "review"
    assert final.loop_counts == {"task-lead": 2}     # review reset, upstream kept



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
    st.step = "review"
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

    args = argparse.Namespace(token="feat", from_target=None)
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


def test_crash_recovery_sets_resume_pending_to_current_step(tmp_path, monkeypatch) -> None:
    """A hard-killed run leaves status='running' on disk and never set
    resume_pending. P1: crash recovery must keep and replay the CURRENT step's
    transcript, so resume_pending is set to the cursor before run_change."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.change import Change
    from imp.engine.executor import RunResult
    from imp.engine.state import State

    _seed_control_default(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "review"
    st.status = "running"          # crashed: status on disk, lock freed by the OS
    st.resume_pending = None
    st.save(ch.state_dir)

    captured: dict[str, object] = {}

    async def fake_run_change(change, **kwargs):
        captured["resume_pending"] = State.load(change.state_dir).resume_pending
        return RunResult.COMPLETED

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    args = argparse.Namespace(token="feat", from_target=None)
    rc = anyio.run(m._async_cmd_resume, args)
    assert rc == 0
    assert captured["resume_pending"] == "review"


def test_resume_non_interactive_routes_through_session_client(
    tmp_path, monkeypatch,
) -> None:
    """Headless resume routes through run_session_client, not running the agent loop in-process."""
    import argparse

    import anyio

    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    async def _fail_run_change(*_a, **_k):
        raise AssertionError("run_change must not run in-process for headless resume")

    monkeypatch.setattr(m, "run_change", _fail_run_change)

    made: dict[str, str | None] = {}

    def _fake_make_headless_solicit(message, _coord):
        made["message"] = message

        async def _solicit(_prompt):
            return message or ""

        return _solicit

    monkeypatch.setattr(
        m, "make_headless_solicit", _fake_make_headless_solicit, raising=False)

    captured: dict[str, object] = {}

    async def _fake_run_session_client(
        change, *, driver, coord, solicit, seed_opening=None, status=None,
        resume=False, from_target=None,
    ) -> int:
        captured["change_id"] = change.id
        captured["resume"] = resume
        captured["from_target"] = from_target
        return 7

    monkeypatch.setattr(m, "run_session_client", _fake_run_session_client)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", from_target=None, non_interactive=True, message="go"))

    assert rc == 7
    assert made["message"] == "go"
    assert captured["change_id"] == ch.id
    assert captured["resume"] is True
    assert captured["from_target"] is None


def test_resume_non_interactive_bad_from_returns_2_without_spawning(
    tmp_path, monkeypatch, capsys,
) -> None:
    """Bad --from returns rc 2 without reaching run_session_client."""
    import argparse

    import anyio

    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    async def _fail_client(*_a, **_k) -> int:
        raise AssertionError("run_session_client must not run on a bad --from")

    monkeypatch.setattr(m, "run_session_client", _fail_client)

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", from_target="no-such-step", non_interactive=True, message="go"))

    assert rc == 2
    assert "no-such-step" in capsys.readouterr().err
