# tests/test_isolated_worktree_dispatch.py
from __future__ import annotations

import types
from pathlib import Path

import anyio
import pytest

from imp.agents.runner import ChangeContext
from imp.agents.subagents import dispatch_task
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from tests.conftest import _git, _seed_control

_SRC = SourceLoc(file=Path("a.md"), line=1)


def _isolated_agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"bash", "write_file"}),
        subagents=(),
        output_schema=OutputSchema(fields=(), source=_SRC),
        prompt_template_source="probe {{ change.id }}",
        source_path=Path("a.md"), source=_SRC,
        isolated_worktree=True)


def _plain_agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(fields=(), source=_SRC),
        prompt_template_source="hi", source_path=Path("a.md"), source=_SRC)


class _FakeRunner:
    def __init__(self, events, *, on_run=None, capture=None):
        self._events = events
        self._on_run = on_run
        self._capture = capture

    async def run(self, inv):
        if self._capture is not None:
            self._capture["inv"] = inv
        if self._on_run is not None:
            self._on_run(inv)
        for e in self._events:
            yield e

    async def interrupt(self) -> None: ...


def _spawn(events, *, on_run=None, capture=None):
    async def _s(_inv):
        return _FakeRunner(events, on_run=on_run, capture=capture)
    return _s


def _make_change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="T", change_id="prb001",
                         base_branch="main")


def _ctx(tmp_path: Path, change: Change, *, name: str, agent: AgentDef,
         spawn) -> ToolContext:
    ctx = ToolContext(project_root=change.project_root,
                      events_path=change.events_path)
    ctx.subagent_defs = {name: agent}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude"
    ctx.parent_transcript_dir = tmp_path / "steps" / "p" / "s"
    ctx.coordinator = InterruptCoordinator()
    ctx.change = change
    ctx.change_context = ChangeContext(
        id=change.id, title=change.title, project_root=change.project_root,
        change_home=f".imp/changes/{change.id}",
        workflow_dir=change.workflow_dir)
    ctx.sandbox_policy = None
    ctx.spawn_subagent = spawn
    return ctx


def test_isolated_dispatch_runs_in_throwaway_with_distinct_policy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force an ACTIVE policy build regardless of whether the host has bwrap; the
    # fake runner never actually spawns a sandboxed shell, so the policy is
    # inspected as pure data.
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    change = _make_change(tmp_path)
    capture: dict = {}
    ctx = _ctx(tmp_path, change, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_output", "payload": {"text": "ok"}}],
                            capture=capture))

    result = anyio.run(dispatch_task, ctx, "prober", "measure it")
    assert result == "ok"

    inv = capture["inv"]
    throwaway = (tmp_path / ".imp" / "worktrees" / "spike-prb001-1").resolve()
    # the subagent's working root is the disposable worktree
    assert inv.change_context.project_root == throwaway
    pol = inv.sandbox_policy
    assert pol.network is True
    assert pol.worktree == str(throwaway)
    # the DURABLE change home is the rw-bound change dir (survives teardown)
    assert pol.change_home == str(change.change_dir)
    # teardown: throwaway worktree + its branch are gone
    assert not throwaway.exists()
    assert "imp/spike-prb001-1" not in _git(
        tmp_path, "branch", "--list", "imp/spike-prb001-1").stdout


def test_isolated_dispatch_parks_artifact_surviving_teardown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    change = _make_change(tmp_path)

    seen: dict = {}

    def _park(inv) -> None:
        seen["root"] = inv.change_context.project_root
        # write through the planted change-home symlink inside the throwaway
        home = inv.change_context.project_root / ".imp" / "changes" / change.id
        dest = home / "artifacts" / "spike" / "finding.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("empirical result: 42\n")

    ctx = _ctx(tmp_path, change, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_output", "payload": {"text": "done"}}],
                            on_run=_park))

    anyio.run(dispatch_task, ctx, "prober", "park a finding")

    throwaway = (tmp_path / ".imp" / "worktrees" / "spike-prb001-1").resolve()
    # the write happened INSIDE the disposable worktree ...
    assert seen["root"] == throwaway
    # ... which is now gone, yet the parked artifact persists in the durable home
    parked = change.change_dir / "artifacts" / "spike" / "finding.md"
    assert parked.read_text() == "empirical result: 42\n"
    assert not throwaway.exists()


def test_isolated_dispatch_tears_down_on_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.engine.worktree as wtmod

    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    change = _make_change(tmp_path)

    # Spy on teardown so the test proves it ran with the spike's id+discriminator
    # (a bare "worktree is absent" check can't tell "never created" from
    # "created then torn down"). Delegates to the real destroyer so the throwaway
    # is genuinely removed.
    calls: list = []
    real_destroy = wtmod.destroy_spike_worktree

    def _spy(control_root, change_id, discriminator):
        calls.append((change_id, discriminator))
        return real_destroy(control_root, change_id, discriminator)

    monkeypatch.setattr(wtmod, "destroy_spike_worktree", _spy)

    async def _spawn_boom(_inv):
        raise RuntimeError("spike spawn failed")

    ctx = _ctx(tmp_path, change, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn_boom)

    with pytest.raises(RuntimeError, match="spike spawn failed"):
        anyio.run(dispatch_task, ctx, "prober", "will fail")

    # teardown ran (with the spike's id + discriminator) despite the dispatch error
    assert calls == [("prb001", 1)]
    assert not (tmp_path / ".imp" / "worktrees" / "spike-prb001-1").exists()
    assert "imp/spike-prb001-1" not in _git(
        tmp_path, "branch", "--list", "imp/spike-prb001-1").stdout


def test_non_isolated_subagent_inherits_parent_policy(tmp_path: Path) -> None:
    change = _make_change(tmp_path)
    capture: dict = {}
    sentinel = object()
    ctx = _ctx(tmp_path, change, name="rev", agent=_plain_agent("rev"),
               spawn=_spawn([{"type": "agent_output", "payload": {"text": "ok"}}],
                            capture=capture))
    ctx.sandbox_policy = sentinel

    anyio.run(dispatch_task, ctx, "rev", "review")

    inv = capture["inv"]
    assert inv.sandbox_policy is sentinel                 # inherited unchanged
    assert inv.change_context.project_root == change.project_root
    # no spike worktree was ever created
    assert list((tmp_path / ".imp" / "worktrees").glob("spike-*")) == []


def test_inactive_spike_policy_emits_sandbox_inactive_event(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inactive_policy = types.SimpleNamespace(active=False, note="unsandboxed_by_config")
    monkeypatch.setattr(
        "imp.agents.subagents._build_spike_policy", lambda *_a, **_kw: inactive_policy)

    change = _make_change(tmp_path)
    ctx = _ctx(tmp_path, change, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_output", "payload": {"text": "ok"}}]))
    ctx.agent_id = "prober"

    recorded: list[dict] = []

    class _CapturingLog:
        def append(self, *, agent_id, type, payload):
            recorded.append({"agent_id": agent_id, "type": type, "payload": payload})

    ctx.event_log = _CapturingLog()

    anyio.run(dispatch_task, ctx, "prober", "measure it")

    sandbox_events = [e for e in recorded if e["type"] == "sandbox_inactive"]
    assert len(sandbox_events) == 1
    assert sandbox_events[0]["payload"]["reason"] == "unsandboxed_by_config"
    assert sandbox_events[0]["agent_id"] == "prober"


def test_isolated_dispatch_without_change_context_raises(tmp_path: Path) -> None:
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    ctx.subagent_defs = {"prober": _isolated_agent("prober")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude"
    ctx.parent_transcript_dir = tmp_path / "steps" / "p" / "s"
    ctx.coordinator = InterruptCoordinator()
    ctx.change = None                                     # nested-dispatch shape
    ctx.change_context = ChangeContext(
        id="x", title="X", project_root=tmp_path, workflow_dir=tmp_path / "wf")
    ctx.spawn_subagent = _spawn([{"type": "agent_output", "payload": {"text": "ok"}}])

    with pytest.raises(RuntimeError, match="isolated_worktree"):
        anyio.run(dispatch_task, ctx, "prober", "go")
