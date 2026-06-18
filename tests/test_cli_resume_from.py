import pytest

from imp.cli.main import main
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _git, _seed_control


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


def test_resume_interactive_routes_through_session_client(
    tmp_path, monkeypatch,
) -> None:
    """Interactive `imp resume` routes through run_session_client with resume=True;
    no in-process ESC reader is spawned; no run_change call is made in-process."""
    import argparse

    import anyio

    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    made: dict[str, object] = {}

    def _fake_make_interactive_solicit(change, *, status, driver, coord):
        made["change_id"] = change.id

        async def _solicit(_prompt):
            return ""

        return _solicit

    monkeypatch.setattr(
        m, "make_interactive_solicit", _fake_make_interactive_solicit)

    captured: dict[str, object] = {}

    async def _fake_run_session_client(
        change, *, driver, coord, solicit, seed_opening=None, status=None,
        resume=False, from_target=None,
    ) -> int:
        captured["change_id"] = change.id
        captured["resume"] = resume
        captured["from_target"] = from_target
        return 5

    monkeypatch.setattr(m, "run_session_client", _fake_run_session_client)
    monkeypatch.setattr(m, "_select_headless", lambda _args, **_kw: (False, False))

    rc = anyio.run(m._async_cmd_resume, argparse.Namespace(
        token="feat", from_target=None))

    assert rc == 5
    assert made["change_id"] == captured["change_id"]   # solicit bound to the same change
    assert captured["resume"] is True
    assert captured["from_target"] is None


def test_rewind_note_line_states_from_and_to() -> None:
    from imp.cli.main import _rewind_note_line
    assert _rewind_note_line("plan", "spec", color=False) == \
        "rewound from plan to spec"
    # A None cursor (rewind to the very start) reads as 'start'.
    assert _rewind_note_line(None, "spec", color=False) == \
        "rewound from start to spec"


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
