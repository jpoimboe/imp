from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from imp.cli.mutation_proxy import build_session_handler
from imp.engine.quest import Quest
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _seed_control

_BODY_TERM = "zarquon9481"
_BACKLOG = "bklog1"
_ARCHIVED = "arch7"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _archive_abandoned_backlog(control: Path, staging: Path) -> None:
    """Archive: materialized:false/origin:backlog — queued brief if classification reads archive."""
    from imp.engine import archive

    staging.mkdir()
    events = staging / "events.jsonl"
    events.write_text("")
    qj = staging / "quest.json"
    qj.write_text(json.dumps({
        "id": _ARCHIVED, "title": "Shelved idea", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": False, "origin": "backlog",
        "seed_opening": "Shelved idea\n\nold body"}))
    sj = staging / "state.json"
    sj.write_text(json.dumps({"id": _ARCHIVED, "status": "abandoned", "step": None}))
    archive.archive_quest(control, _ARCHIVED, events_path=events,
                          quest_json_path=qj, state_json_path=sj)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """(daemon-side control root holding the quests, client control root holding none)."""
    server = tmp_path / "server"
    server.mkdir()
    _seed_control(server)
    Quest.create_draft(
        server, title="Queued brief", quest_id=_BACKLOG, base_branch="main",
        origin="backlog", seed_opening=f"Queued brief\n\n{_BODY_TERM}",
        defer_seed=True)
    _archive_abandoned_backlog(server, tmp_path / "staging")
    client = tmp_path / "client"
    client.mkdir()
    _seed_control(client)
    return server, client


async def _imp_over_daemon(server: Path, client: Path, sock: str,
                           *args: str) -> tuple[int, str, str]:
    """Mirrors the sandbox topology (local state unreadable, daemon authoritative) with no bwrap."""
    session = SessionSocket(sock, on_query=build_session_handler(server))
    env = {**os.environ, "IMP_SESSION_SOCKET": sock, "IMP_PROJECT_ROOT": str(client)}
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        try:
            result = await anyio.run_process(
                [sys.executable, "-m", "imp", *args],
                cwd=str(client), env=env, check=False)
        finally:
            await session.close()
            tg.cancel_scope.cancel()
    return (result.returncode, result.stdout.decode(errors="replace"),
            result.stderr.decode(errors="replace"))


def _fields(out: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, _, v in
            (line.partition(":") for line in out.splitlines() if ":" in line)}


def test_client_root_holds_no_quest_of_its_own(roots: tuple[Path, Path]) -> None:
    _server, client = roots
    env = {k: v for k, v in os.environ.items() if k != "IMP_SESSION_SOCKET"}
    p = subprocess.run([sys.executable, "-m", "imp", "show", _BACKLOG],
                       cwd=str(client), env=env, capture_output=True, text=True)
    assert p.returncode == 2
    assert f"no quest matches {_BACKLOG!r}" in p.stderr


@pytest.mark.anyio
async def test_backlog_card_renders_through_the_daemon(
        roots: tuple[Path, Path], tmp_path: Path) -> None:
    server, client = roots
    rc, out, err = await _imp_over_daemon(
        server, client, str(tmp_path / "s.sock"), "show", _BACKLOG)
    assert rc == 0, err
    fields = _fields(out)
    assert fields["id"] == _BACKLOG
    assert fields["title"] == "Queued brief"
    assert fields["status"] == "backlog"
    assert fields["provenance"] == "backlog"
    assert fields["readiness"] == "ready"
    assert out.splitlines()[-2:] == ["intent:", _BODY_TERM]


@pytest.mark.anyio
async def test_archived_id_renders_the_archive_card(
        roots: tuple[Path, Path], tmp_path: Path) -> None:
    server, client = roots
    rc, out, err = await _imp_over_daemon(
        server, client, str(tmp_path / "s.sock"), "show", _ARCHIVED)
    assert rc == 0, err
    fields = _fields(out)
    assert fields["id"] == _ARCHIVED
    assert fields["title"] == "Shelved idea"
    assert fields["status"] == "abandoned"
    assert "backlog" not in out
