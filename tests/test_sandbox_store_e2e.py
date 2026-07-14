"""End-to-end in-sandbox store proxy + masking integration (spec AC 2 a-g).

Stands up a REAL session daemon serving the CLI introspection commands through the
ProxyStore socket transport, with the raw internal-state files masked by REAL bwrap.
Proves that inside the sandbox each `imp` read command returns correct data via the
daemon while the raw files (events.jsonl, archive/blobs, archive/index.db) are
unreadable — across current, live-sibling, dormant, and archived targets, exercising
every named store op.

Modeled on tests/test_sandbox_behavioral.py (real bwrap policy) and the quest's spike
(.imp/quests/rifa59/artifacts/spike/faithful.py: live SessionSocket concurrent with an
awaited bwrap subprocess that connects back over the socket). Gated by
sandbox_available() — the same gate the behavioral suite runs green under here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
import pytest

from imp.cli.store_proxy import build_query_handler
from imp.engine import archive
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.sandbox import (
    SandboxConfig,
    build_sandbox_argv,
    prune_missing_masks,
    resolve_policy,
    sandbox_available,
)
from imp.engine.state import State
from imp.remote.session.discover import session_socket_path
from imp.remote.session.socket_server import SessionSocket

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable")

# src/ on the child's PYTHONPATH so in-sandbox `python -m imp` resolves the
# working-tree package (mirrors conftest's subprocess PYTHONPATH pin).
_SRC = str(Path(__file__).resolve().parents[1] / "src")

# Quest ids under test. CUR is the agent's own quest (its socket is injected as
# IMP_SESSION_SOCKET and its daemon serves every command); the rest are targets the
# daemon reads from disk / archive.
CUR = "cur-quest"
LIVE_SIB = "live-sib"
DORM_SIB = "dorm-sib"
ARCH = "arch-quest"
BLK = "blk-quest"

# Short, unique, per-quest markers. `imp events <id> --all` takes the leaf path
# (main.py:3445 `leaf = bool(... or args.all ...)`) and emits one per-event digest
# line per event (main.py:3478-3490); the opening_intent renders as `text=<marker>`,
# so the marker appears in stdout. Bare `imp events <id>` (no --all) would instead
# print only the summary index (header + steps table + types histogram, main.py:3468-
# 3476) with no payload text — hence every event-path invocation below passes --all.
# Each marker lives ONLY in that quest's masked events.jsonl (or archive blob), except
# SPEC_MARK which lives only in the current quest's spec artifact — so a sandbox grep
# that finds a marker means the mask leaked.
CUR_MARK = "CURMARK"
SIB_MARK = "SIBMARK"
DORM_MARK = "DORMARK"
ARCH_MARK = "ARCHMARK"
SPEC_MARK = "SPECMARK"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main` and `.imp/` gitignored, so the
    in-sandbox CLI's `git rev-parse --git-common-dir` control-root resolution works."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _events(marker: str) -> list[dict[str, object]]:
    """An opening_intent (renders in `imp events`) + a human user_message (feeds
    conversation prose for `imp search`'s active/archived zones), both carrying the
    marker."""
    return [
        {"id": "e1", "timestamp": "2026-06-01T00:00:01+00:00",
         "type": "opening_intent", "payload": {"text": marker}},
        {"id": "e2", "timestamp": "2026-06-01T00:00:02+00:00",
         "type": "user_message",
         "payload": {"human": True,
                     "content": [{"type": "text", "text": marker}]}},
    ]


def _write_quest(
    control: Path, qid: str, *, title: str,
    events: list[dict[str, object]],
    status: str = "new",
    spec: str | None = None,
    materialized: bool = True,
    origin: str = "draft",
    depends_on: tuple[str, ...] = (),
    write_state: bool = True,
) -> Path:
    """Write a live quest home under `.imp/quests/<qid>/` (quest.json + optional
    state.json + events.jsonl + optional spec artifact). Returns the home dir."""
    d = control / ".imp" / "quests" / qid
    (d / "state").mkdir(parents=True, exist_ok=True)
    meta = {
        "id": qid, "title": title, "base_branch": "main",
        "project_root": str(control), "layout_version": LAYOUT_VERSION,
        "created_at": "2026-06-01T00:00:00+00:00",
        "materialized": materialized, "origin": origin,
        "depends_on": list(depends_on),
    }
    (d / "quest.json").write_text(json.dumps(meta))
    if write_state:
        State(id=qid, step="implement", step_attempt=0, status=status).save(d / "state")
    with (d / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    if spec is not None:
        art = d / "artifacts" / "spec"
        art.mkdir(parents=True, exist_ok=True)
        (art / f"{qid}.md").write_text(spec)
    return d


def _write_archived_quest(control: Path, qid: str, *, title: str, marker: str) -> None:
    """Build a completed quest home, archive it (records/ + blobs/ + index.db), then
    remove the live home so the id resolves ONLY through the archive — get_events for
    it must go through archive_read (blob), and its content must NOT be readable raw
    (blobs/ + index.db are masked)."""
    d = _write_quest(control, qid, title=title, events=_events(marker),
                     status="completed")
    archive.archive_quest(
        control, qid,
        events_path=d / "events.jsonl",
        quest_json_path=d / "quest.json",
        state_json_path=d / "state" / "state.json")
    shutil.rmtree(d)


def _build_project(base: Path) -> Path:
    """A real git-repo control root with five quests covering AC 2's target categories.

    - CUR: current quest (its daemon serves every command; events.jsonl masked).
    - LIVE_SIB / DORM_SIB: sibling live quests the daemon reads from disk (both
      masked). The in-sandbox CLI always talks to its OWN daemon, which reads siblings
      unsandboxed — so 'live' vs 'dormant' are indistinguishable to the CLI path and
      are modeled as two on-disk siblings (see Decisions).
    - ARCH: archived quest (blobs/ + index.db masked; served via archive_read).
    - BLK: backlog quest (materialized False, origin 'backlog') depending on a never-
      archived id, so backlog_readiness classifies it blocked.
    """
    control = base
    _init_repo(control)
    _write_quest(control, CUR, title="Current", events=_events(CUR_MARK),
                 spec=f"# spec\n\n{SPEC_MARK} current spec body\n")
    _write_quest(control, LIVE_SIB, title="Live Sibling", events=_events(SIB_MARK))
    _write_quest(control, DORM_SIB, title="Dormant Sibling", events=_events(DORM_MARK))
    _write_archived_quest(control, ARCH, title="Archived Thing", marker=ARCH_MARK)
    _write_quest(control, BLK, title="Blocked Backlog", events=[],
                 materialized=False, origin="backlog",
                 depends_on=("ghost-dep",), write_state=False)
    return control


@pytest.fixture(scope="module")
def project():
    base = Path(tempfile.mkdtemp(prefix="imp-store-e2e-"))
    try:
        yield _build_project(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def _serve_and_run(
    control: Path, command: str, *, timeout: float = 90.0,
) -> tuple[int | None, str, str]:
    """Serve CUR's real daemon (build_query_handler) concurrently while running
    `command` in a REAL bwrap sandbox whose policy comes from resolve_policy (masks
    events.jsonl / archive blobs / index.db and injects IMP_SESSION_SOCKET). The
    in-sandbox command connects back to the daemon over the socket (the reentrant
    self-read). Returns (returncode, stdout, stderr). Asserts no timeout/deadlock."""
    from subprocess import PIPE

    engine_env = {"PYTHONPATH": _SRC}
    cfg = SandboxConfig(enabled=True, allow_unsandboxed=False, network=True,
                        uid=os.getuid())
    policy = resolve_policy(
        worktree=control, control_root=control, quest_id=CUR, cfg=cfg,
        engine_env_result=engine_env, environ=dict(os.environ),
        available=True, disposable_tree=True)
    # Mirror the real spawn path (bash.py / gates.py): existence-gate the frozen
    # masks before handing them to bwrap, so a mask over a path absent on this host
    # (e.g. /run/user/$UID when there is no live user session) doesn't die in sandbox
    # setup. The masks under test (events.jsonl, archive blobs, index.db) all exist
    # and survive the prune, so the asserted behavior is unchanged.
    policy = prune_missing_masks(policy)
    argv = build_sandbox_argv(
        command, cwd=str(control), policy=policy, engine_env=engine_env)

    sock_path = session_socket_path(control, CUR)
    socket = SessionSocket(str(sock_path), on_query=build_query_handler(control))
    await socket.open()

    out: dict[str, object] = {"rc": None, "stdout": b"", "stderr": b""}

    async def serve() -> None:
        await socket.serve()

    async def spawn() -> None:
        # `async with` so a deadlock-path cancellation (move_on_after) terminates the
        # bwrap child instead of leaking it.
        async with await anyio.open_process(argv, stdout=PIPE, stderr=PIPE) as proc:
            oc: list[bytes] = []
            ec: list[bytes] = []

            async def drain(stream, sink):
                if stream is None:
                    return
                async for chunk in stream:
                    sink.append(chunk)

            async with anyio.create_task_group() as tg2:
                tg2.start_soon(drain, proc.stdout, oc)
                tg2.start_soon(drain, proc.stderr, ec)
                await proc.wait()
            out["rc"] = proc.returncode
            out["stdout"] = b"".join(oc)
            out["stderr"] = b"".join(ec)

    try:
        with anyio.move_on_after(timeout) as scope:
            async with anyio.create_task_group() as tg:
                tg.start_soon(serve)
                await spawn()
                tg.cancel_scope.cancel()
        timed_out = scope.cancelled_caught
    finally:
        await socket.close()

    assert not timed_out, f"sandboxed `{command}` timed out (possible deadlock)"
    return (
        out["rc"],  # type: ignore[return-value]
        out["stdout"].decode(errors="replace"),  # type: ignore[union-attr]
        out["stderr"].decode(errors="replace"),  # type: ignore[union-attr]
    )


def _imp(control: Path, *args: str) -> tuple[int | None, str, str]:
    command = " ".join([sys.executable, "-m", "imp", *args])
    return anyio.run(_serve_and_run, control, command)


def test_current_events_served_and_raw_masked(project) -> None:
    """AC 2(a) + AC 1 + AC 8: `imp events <cur>` returns the current quest's events
    THROUGH the daemon-proxied store while the raw events.jsonl is masked in-sandbox,
    and the reentrant self-read (CLI connecting back to the daemon that awaits it)
    completes without deadlock (the harness asserts no timeout)."""
    control = project
    rc, out, err = _imp(control, "events", CUR, "--all")
    assert rc == 0, f"imp events failed: rc={rc} err={err!r}"
    assert CUR_MARK in out, f"proxied events missing marker: {out!r}"

    # Raw masking: the marker lives ONLY in the masked events.jsonl, so an in-sandbox
    # recursive grep must not find it.
    _, scan, _ = anyio.run(
        _serve_and_run, control,
        "grep -rn CURMARK .imp/quests 2>/dev/null; echo SCAN_DONE")
    assert "SCAN_DONE" in scan
    assert CUR_MARK not in scan, "events.jsonl not masked inside sandbox — mask leaked"
