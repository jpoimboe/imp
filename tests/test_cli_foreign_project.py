from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

import anyio
import pytest

from imp.cli.mutation_proxy import build_session_handler
from imp.engine.quest import Quest, live_quest_ids
from imp.paths import display_path
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _git, _init_repo, _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Projects(NamedTuple):
    run: Path      # A's main checkout: the daemon's control root
    run_wt: Path   # linked worktree of A: the run's IMP_PROJECT_ROOT
    foreign: Path  # B: a separate git repo


@pytest.fixture
def projects(tmp_path: Path) -> _Projects:
    run = tmp_path / "a"
    run.mkdir()
    _seed_control(run)
    run_wt = tmp_path / "a-wt"
    added = _git(run, "worktree", "add", "-q", "-b", "imp/run", str(run_wt), "main")
    assert added.returncode == 0, added.stderr
    foreign = tmp_path / "b"
    foreign.mkdir()
    _init_repo(foreign)
    return _Projects(run, run_wt, foreign)


async def _imp_in_run(projects: _Projects, cwd: Path, sock: str, *args: str,
                      extra_env: dict[str, str] | None = None,
                      ) -> tuple[int, str, str, list[str]]:
    """`imp *args` from `cwd` as a sandboxed agent of a run on A; returns rc/out/err/ops."""
    inner = build_session_handler(projects.run)
    ops: list[str] = []

    async def _on_query(op: str, qargs: dict[str, Any]) -> Any:
        ops.append(op)
        return await inner(op, qargs)

    session = SessionSocket(sock, on_query=_on_query)
    env = {**os.environ, "IMP_SESSION_SOCKET": sock,
           "IMP_PROJECT_ROOT": str(projects.run_wt), **(extra_env or {})}
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        try:
            result = await anyio.run_process(
                [sys.executable, "-m", "imp", *args],
                cwd=str(cwd), env=env, check=False)
        finally:
            await session.close()
            tg.cancel_scope.cancel()
    return (result.returncode, result.stdout.decode(errors="replace"),
            result.stderr.decode(errors="replace"), ops)


@pytest.mark.anyio
@pytest.mark.parametrize("args", [
    pytest.param(["new", "--backlog", "Scratch idea"], id="new"),
    pytest.param(["list"], id="list"),
    pytest.param(["edit", "zz", "--title", "T"], id="edit"),
    pytest.param(["abandon", "zz"], id="abandon"),
    pytest.param(["resume", "zz"], id="resume"),
])
async def test_refused_from_a_foreign_project(
        projects: _Projects, tmp_path: Path, args: list[str]) -> None:
    rc, _out, err, ops = await _imp_in_run(
        projects, projects.foreign, str(tmp_path / "s.sock"), *args)
    assert rc == 2
    assert err == (
        f"imp: {display_path(projects.foreign.resolve())} is a different project "
        f"from this run's ({display_path(projects.run.resolve())})\n"
        "  to use that project's own imp state, clear the run's pins: "
        "IMP_SESSION_SOCKET= IMP_STATE_DIR= imp …\n")
    assert ops == []
    assert live_quest_ids(projects.run) == []


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["run_wt", "run"])
async def test_new_still_relays_from_the_runs_own_project(
        projects: _Projects, tmp_path: Path, where: str) -> None:
    rc, _out, err, ops = await _imp_in_run(
        projects, getattr(projects, where), str(tmp_path / "s.sock"),
        "new", "--backlog", "Scratch idea")
    assert rc == 0, err
    assert ops == ["new"]
    assert len(live_quest_ids(projects.run)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["run_wt", "run"])
async def test_list_still_relays_from_the_runs_own_project(
        projects: _Projects, tmp_path: Path, where: str) -> None:
    Quest.create_draft(
        projects.run, title="Board item", quest_id="bord1", base_branch="main",
        origin="backlog", seed_opening="Board item\n\nbody", defer_seed=True)
    rc, out, err, ops = await _imp_in_run(
        projects, getattr(projects, where), str(tmp_path / "s.sock"), "list")
    assert rc == 0, err
    assert "bord1" in out
    assert ops  # served by the daemon, not read locally


@pytest.mark.anyio
async def test_new_still_relays_from_outside_any_repo(
        projects: _Projects, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, _out, err, ops = await _imp_in_run(
        projects, plain, str(tmp_path / "s.sock"), "new", "--backlog", "Scratch idea",
        extra_env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert rc == 0, err
    assert ops == ["new"]
    assert len(live_quest_ids(projects.run)) == 1
