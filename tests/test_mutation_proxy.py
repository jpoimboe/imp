from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import anyio
import pytest

import imp.remote.daemon.spawn as spawn_mod
from imp.cli import main
from imp.cli.mutation_proxy import build_session_handler, proxy_mutation, run_mutation
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _branch_with_commit, _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_run_mutation_resume_delegates_to_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_resume(control_root: Path, token: str) -> str:
        seen["control_root"] = control_root
        seen["token"] = token
        return "Resuming @questa…"

    monkeypatch.setattr(spawn_mod, "resume_quest", _fake_resume)
    result = run_mutation(tmp_path, "resume", {"token": "questa"})
    assert result == {"returncode": 0, "message": "Resuming @questa…"}
    assert seen == {"control_root": tmp_path, "token": "questa"}


def test_run_mutation_unknown_op_raises() -> None:
    with pytest.raises(ValueError):
        run_mutation(Path("/nope"), "explode", {})


def _draft(control: Path, quest_id: str) -> Quest:
    return Quest.create_draft(
        control, title="Original", quest_id=quest_id, base_branch="main",
        origin="draft")


def _meta(control: Path, quest_id: str) -> dict:
    quest = Quest.load(control, quest_id)
    return json.loads((quest.quest_dir / "quest.json").read_text())


def test_run_mutation_edit_sets_depends_on(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-mut10")
    result = run_mutation(tmp_path, "edit", {
        "token": "mut10", "title": None, "backlog": False, "active": False,
        "depends_on": ["dep77"], "clear_deps": False, "append_intent": None, "base": None})
    assert result == {
        "returncode": 0,
        "message": "edited 2026-06-24-mut10 · depends on dep77"}
    assert _meta(tmp_path, "2026-06-24-mut10")["depends_on"] == ["dep77"]


def test_run_mutation_edit_no_match_returns_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    result = run_mutation(tmp_path, "edit", {
        "token": "ghost", "title": "X", "backlog": False, "active": False,
        "depends_on": None, "clear_deps": False, "append_intent": None, "base": None})
    assert result["returncode"] == 2
    assert "no quest matches" in result["message"]


def test_run_mutation_edit_ambiguous_returns_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-ambig10")
    _draft(tmp_path, "2026-06-24-ambig20")
    result = run_mutation(tmp_path, "edit", {
        "token": "ambig", "title": "X", "backlog": False, "active": False,
        "depends_on": None, "clear_deps": False, "append_intent": None, "base": None})
    assert result["returncode"] == 2
    assert "is ambiguous" in result["message"]


def test_run_mutation_edit_rejects_materialized_deps(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest = _draft(tmp_path, "2026-06-24-mut15")
    quest.set_depends_on(["A"])
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True))
    result = run_mutation(tmp_path, "edit", {
        "token": "mut15", "title": None, "backlog": False, "active": False,
        "depends_on": ["B"], "clear_deps": False, "append_intent": None, "base": None})
    assert result["returncode"] == 2
    assert result["message"] == "imp: cannot change dependencies once a quest is built"
    assert _meta(tmp_path, "2026-06-24-mut15")["depends_on"] == ["A"]


def test_run_mutation_edit_sets_base(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-mut18")
    tip = _branch_with_commit(tmp_path, "release-2.0")
    result = run_mutation(tmp_path, "edit", {
        "token": "mut18", "title": None, "backlog": False, "active": False,
        "depends_on": None, "clear_deps": False, "append_intent": None,
        "base": "release-2.0"})
    assert result["returncode"] == 0
    assert "base → release-2.0" in result["message"]
    meta = _meta(tmp_path, "2026-06-24-mut18")
    assert meta["base_branch"] == "release-2.0"
    assert meta["orig_base_head"] == tip


def test_run_mutation_new_records_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    result = run_mutation(tmp_path, "new", {
        "opening": "brief text", "title": None, "workflow": None, "base": None,
        "idea": "From the agent", "depends_on": None, "var_flags": None})
    assert result["returncode"] == 0
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
    qid = quests[-1].name
    assert result["message"] == (
        f"Recorded backlog quest {qid} (start it with `imp resume {qid}`).")
    assert Quest.load(tmp_path, qid).title == "From the agent"


@pytest.mark.anyio
async def test_proxy_mutation_edit_round_trips_over_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-mut50")
    sock = str(tmp_path / "s.sock")
    monkeypatch.setenv("IMP_SESSION_SOCKET", sock)

    session = SessionSocket(sock, on_query=build_session_handler(tmp_path))
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)

        result = await anyio.to_thread.run_sync(
            proxy_mutation, "edit", {
                "token": "mut50", "title": "Renamed via socket", "backlog": False,
                "active": False, "depends_on": None, "clear_deps": False,
                "append_intent": None, "base": None})
        assert result == {
            "returncode": 0,
            "message": 'edited 2026-06-24-mut50 · title → "Renamed via socket"'}
        assert Quest.load(tmp_path, "2026-06-24-mut50").title == "Renamed via socket"

        await session.close()
        tg.cancel_scope.cancel()


def test_cmd_edit_proxies_when_socket_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    seen: dict[str, object] = {}

    def _fake(op: str, args: dict) -> dict:
        seen["op"] = op
        seen["args"] = args
        return {"returncode": 0, "message": ""}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation", _fake)
    args = argparse.Namespace(
        token="q1", title="New", backlog=False, active=False,
        depends_on=None, clear_deps=False, append_intent=None,
        base="release-2.0")
    assert main._cmd_edit(args) == 0
    assert seen["op"] == "edit"
    assert seen["args"]["token"] == "q1"
    assert seen["args"]["title"] == "New"
    # The daemon reads args["base"] unconditionally, so a dropped key is a KeyError.
    assert seen["args"]["base"] == "release-2.0"


def test_async_cmd_resume_proxies_when_socket_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    seen: dict[str, object] = {}

    async def _fake(op: str, args: dict) -> dict:
        seen["op"] = op
        seen["args"] = args
        return {"returncode": 0, "message": "Resuming @q1…"}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation_async", _fake)
    args = argparse.Namespace(token="q1")
    assert asyncio.run(main._async_cmd_resume(args)) == 0
    assert seen == {"op": "resume", "args": {"token": "q1"}}


def test_async_cmd_new_proxies_when_socket_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    seen: dict[str, object] = {}

    async def _fake(op: str, args: dict) -> dict:
        seen["op"] = op
        seen["args"] = args
        return {"returncode": 0, "message": "Recorded backlog quest q9"}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation_async", _fake)
    args = argparse.Namespace(
        opening="brief", title=None, workflow=None, base=None,
        idea="Idea", depends_on=["dep1"])
    assert asyncio.run(main._async_cmd_new(args)) == 0
    assert seen["op"] == "new"
    assert seen["args"]["idea"] == "Idea"
    assert seen["args"]["depends_on"] == ["dep1"]


def test_run_mutation_new_records_parent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    result = run_mutation(tmp_path, "new", {
        "opening": "x", "title": None, "workflow": None, "base": None,
        "idea": "Child", "depends_on": None, "var_flags": None}, "mom99")
    assert result["returncode"] == 0
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
    meta = json.loads((quests[-1] / "quest.json").read_text())
    assert meta["parent"] == "mom99"


def test_run_mutation_new_background_starts_quest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    started: dict[str, object] = {}
    monkeypatch.setattr(
        spawn_mod, "start_quest",
        lambda control, qid: started.update(qid=qid))
    result = run_mutation(tmp_path, "new", {
        "opening": "brief", "title": "T", "workflow": None, "base": None,
        "idea": None, "depends_on": None, "var_flags": None, "background": True})
    assert result["returncode"] == 0
    qid = started["qid"]
    assert result["message"] == (
        f"Started quest {qid} (running autonomously in the background; "
        f"appears in the active list).")


@pytest.mark.anyio
async def test_build_session_handler_threads_parent_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    handler = build_session_handler(tmp_path, parent_id="mom7")
    result = await handler("new", {
        "opening": "x", "title": None, "workflow": None, "base": None,
        "idea": "Child", "depends_on": None, "var_flags": None})
    assert result["returncode"] == 0
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
    meta = json.loads((quests[-1] / "quest.json").read_text())
    assert meta["parent"] == "mom7"


def test_async_cmd_new_chat_mode_does_not_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    called = False

    async def _fake(op: str, args: dict) -> dict:
        nonlocal called
        called = True
        return {"returncode": 0, "message": ""}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation_async", _fake)

    class _Stop(Exception):
        pass

    def _boom(*a, **k):
        raise _Stop

    monkeypatch.setattr("imp.cli.signals.install_process_group", _boom)
    args = argparse.Namespace(
        opening="brief", title=None, workflow=None, base=None,
        idea=None, depends_on=None)
    with pytest.raises(_Stop):
        asyncio.run(main._async_cmd_new(args, leave_mode="chat"))
    assert called is False


def test_run_mutation_abandon_tears_down_target(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-abn10")
    result = run_mutation(tmp_path, "abandon", {"token": "abn10"})
    assert result["returncode"] == 0
    assert "Abandoned" in result["message"]
    assert not (project_state_dir(tmp_path) / "quests" / "2026-06-24-abn10").exists()


def test_run_mutation_abandon_no_match_returns_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    result = run_mutation(tmp_path, "abandon", {"token": "ghost"})
    assert result["returncode"] == 2
    assert "no quest matches" in result["message"]


def test_run_mutation_abandon_ambiguous_returns_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-amb10")
    _draft(tmp_path, "2026-06-24-amb20")
    result = run_mutation(tmp_path, "abandon", {"token": "amb"})
    assert result["returncode"] == 2
    assert "is ambiguous" in result["message"]


@pytest.mark.anyio
async def test_proxy_mutation_abandon_round_trips_over_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-abn50")
    sock = str(tmp_path / "s.sock")
    monkeypatch.setenv("IMP_SESSION_SOCKET", sock)

    session = SessionSocket(sock, on_query=build_session_handler(tmp_path))
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)

        result = await anyio.to_thread.run_sync(
            proxy_mutation, "abandon", {"token": "abn50"})
        assert result["returncode"] == 0
        assert not (project_state_dir(tmp_path) / "quests" / "2026-06-24-abn50").exists()

        await session.close()
        tg.cancel_scope.cancel()


def test_cmd_abandon_proxies_when_socket_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    seen: dict[str, object] = {}

    def _fake(op: str, args: dict) -> dict:
        seen["op"] = op
        seen["args"] = args
        return {"returncode": 0, "message": "Abandoned q1."}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation", _fake)
    assert main._cmd_abandon(argparse.Namespace(token="q1", yes=False)) == 0
    assert seen == {"op": "abandon", "args": {"token": "q1"}}


@pytest.mark.parametrize("token", [None, "   "])
def test_cmd_abandon_missing_token_in_sandbox_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], token: str | None,
) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")

    def _boom(op: str, args: dict) -> dict:
        raise AssertionError("must not proxy or tear down without a token")

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation", _boom)
    rc = main._cmd_abandon(argparse.Namespace(token=token, yes=False))
    assert rc == 2
    assert "needs a quest token" in capsys.readouterr().err


def test_new_mutation_threads_var_flags_to_apply_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_apply_new(control: Path, **kwargs: object) -> tuple[int, str]:
        seen.update(kwargs)
        return 0, "Recorded backlog quest q1"

    monkeypatch.setattr("imp.cli.main.apply_new", _fake_apply_new)

    result = run_mutation(tmp_path, "new", {
        "opening": "go", "title": None, "workflow": None, "base": None,
        "idea": None, "depends_on": None, "var_flags": ["retries=5"],
        "background": False})

    assert result["returncode"] == 0
    assert seen["var_flags"] == ["retries=5"]


def test_async_cmd_new_proxy_forwards_background(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    seen: dict[str, object] = {}

    async def _fake(op: str, args: dict) -> dict:
        seen["op"] = op
        seen["args"] = args
        return {"returncode": 0, "message": "Started quest q9"}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation_async", _fake)
    args = argparse.Namespace(
        opening="brief", title="T", workflow=None, base=None, idea=None,
        depends_on=None, background=True)
    assert asyncio.run(main._async_cmd_new(args)) == 0
    assert seen["op"] == "new"
    assert seen["args"]["background"] is True
