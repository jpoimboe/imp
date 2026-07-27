# tests/test_isolated_worktree_dispatch.py
from __future__ import annotations

import types
from pathlib import Path

import anyio
import pytest

from imp.agents.runner import QuestContext
from imp.agents.subagents import dispatch_task
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.quest import Quest
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from tests.conftest import _branch_with_commit, _git, _seed_control

_SRC = SourceLoc(file=Path("a.md"), line=1)


def _isolated_agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"bash", "write_file"}),
        subagents=(),
        output_schema=OutputSchema(fields=(), source=_SRC),
        prompt_template_source="probe {{ quest.id }}",
        source_path=Path("a.md"), source=_SRC,
        isolated_worktree=True)


def _plain_agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(fields=(), source=_SRC),
        prompt_template_source="hi", source_path=Path("a.md"), source=_SRC)


def _active_home_policy(home: str = "/home/u"):
    from imp.engine.sandbox import SandboxPolicy
    return SandboxPolicy(
        active=True, worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(), setenv=(),
        network=True, home=home,
    )


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


def _make_quest(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="T", quest_id="prb001",
                         base_branch="main")


def _ctx(tmp_path: Path, quest: Quest, *, name: str, agent: AgentDef,
         spawn) -> ToolContext:
    ctx = ToolContext(project_root=quest.project_root,
                      events_path=quest.events_path)
    ctx.subagent_defs = {name: agent}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude"
    ctx.parent_transcript_dir = tmp_path / "steps" / "p" / "s"
    ctx.coordinator = InterruptCoordinator()
    ctx.quest = quest
    ctx.quest_context = QuestContext(
        id=quest.id, title=quest.title, project_root=quest.project_root,
        quest_home=f".imp/quests/{quest.id}",
        workflow_dir=quest.workflow_dir)
    ctx.base_sandbox_policy = None
    ctx.spawn_subagent = spawn
    return ctx


def test_isolated_dispatch_runs_in_throwaway_with_distinct_policy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force an ACTIVE policy build regardless of whether the host has bwrap; the
    # fake runner never actually spawns a sandboxed shell, so the policy is
    # inspected as pure data.
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: False)
    quest = _make_quest(tmp_path)
    capture: dict = {}
    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}],
                            capture=capture))

    result = anyio.run(dispatch_task, ctx, "prober", "measure it")
    assert result == "ok"

    inv = capture["inv"]
    throwaway = (tmp_path / ".imp" / "worktrees" / "spike-prb001-1").resolve()
    # the subagent's working root is the disposable worktree
    assert inv.quest_context.project_root == throwaway
    pol = inv.sandbox_policy
    assert pol.network is True
    assert pol.worktree == str(throwaway)
    # the DURABLE change home is the rw-bound change dir (survives teardown)
    assert pol.quest_home == str(quest.quest_dir)
    # teardown: throwaway worktree + its branch are gone
    assert not throwaway.exists()
    assert "imp/spike-prb001-1" not in _git(
        tmp_path, "branch", "--list", "imp/spike-prb001-1").stdout


def test_isolated_dispatch_parks_artifact_surviving_teardown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: False)
    quest = _make_quest(tmp_path)

    seen: dict = {}

    def _park(inv) -> None:
        seen["root"] = inv.quest_context.project_root
        # write through the planted change-home symlink inside the throwaway
        home = inv.quest_context.project_root / ".imp" / "quests" / quest.id
        dest = home / "artifacts" / "spike" / "finding.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("empirical result: 42\n")

    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "done"}}],
                            on_run=_park))

    anyio.run(dispatch_task, ctx, "prober", "park a finding")

    throwaway = (tmp_path / ".imp" / "worktrees" / "spike-prb001-1").resolve()
    # the write happened INSIDE the disposable worktree ...
    assert seen["root"] == throwaway
    # ... which is now gone, yet the parked artifact persists in the durable home
    parked = quest.quest_dir / "artifacts" / "spike" / "finding.md"
    assert parked.read_text() == "empirical result: 42\n"
    assert not throwaway.exists()


def test_isolated_dispatch_tears_down_on_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.engine.worktree as wtmod

    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: False)
    quest = _make_quest(tmp_path)

    # Spy on teardown so the test proves it ran with the spike's id+discriminator
    # (a bare "worktree is absent" check can't tell "never created" from
    # "created then torn down"). Delegates to the real destroyer so the throwaway
    # is genuinely removed.
    calls: list = []
    real_destroy = wtmod.destroy_spike_worktree

    def _spy(control_root, quest_id, discriminator):
        calls.append((quest_id, discriminator))
        return real_destroy(control_root, quest_id, discriminator)

    monkeypatch.setattr(wtmod, "destroy_spike_worktree", _spy)

    async def _spawn_boom(_inv):
        raise RuntimeError("spike spawn failed")

    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn_boom)

    with pytest.raises(RuntimeError, match="spike spawn failed"):
        anyio.run(dispatch_task, ctx, "prober", "will fail")

    # teardown ran (with the spike's id + discriminator) despite the dispatch error
    assert calls == [("prb001", 1)]
    assert not (tmp_path / ".imp" / "worktrees" / "spike-prb001-1").exists()
    assert "imp/spike-prb001-1" not in _git(
        tmp_path, "branch", "--list", "imp/spike-prb001-1").stdout


def test_isolated_dispatch_overlay_home_torn_down_on_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # AC 6 (error path): the overlay cleanup lives in the outer finally, so a
    # failing spawn still removes the per-invocation overlay dirs. Overlay is ON
    # here (unlike test_isolated_dispatch_tears_down_on_error), so this fails if
    # home_cleanup() is ever moved out of finally into the try body.
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: True)
    quest = _make_quest(tmp_path)

    async def _spawn_boom(_inv):
        raise RuntimeError("spike spawn failed")

    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn_boom)

    with pytest.raises(RuntimeError, match="spike spawn failed"):
        anyio.run(dispatch_task, ctx, "prober", "will fail")

    worktrees = (tmp_path / ".imp" / "worktrees").resolve()
    # the per-invocation overlay dirs are gone despite the dispatch error ...
    assert not (worktrees / "spike-prb001-1.overlay-upper").exists()
    assert not (worktrees / "spike-prb001-1.overlay-work").exists()
    assert not (worktrees / "spike-prb001-1.overlay-merged").exists()
    # ... and so is the throwaway worktree
    assert not (worktrees / "spike-prb001-1").exists()


def test_non_isolated_subagent_inherits_parent_policy(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path)
    capture: dict = {}
    sentinel = _active_home_policy()
    ctx = _ctx(tmp_path, quest, name="rev", agent=_plain_agent("rev"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}],
                            capture=capture))
    ctx.base_sandbox_policy = sentinel

    anyio.run(dispatch_task, ctx, "rev", "review")

    inv = capture["inv"]
    assert inv.sandbox_policy is sentinel                 # inherited unchanged
    assert inv.quest_context.project_root == quest.project_root
    # no spike worktree was ever created
    assert list((tmp_path / ".imp" / "worktrees").glob("spike-*")) == []


def test_dispatch_task_forwards_ctx_pythonpath_to_invocation(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path)
    capture: dict = {}
    ctx = _ctx(tmp_path, quest, name="rev", agent=_plain_agent("rev"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}],
                            capture=capture))
    ctx.pythonpath = ("src",)

    anyio.run(dispatch_task, ctx, "rev", "check pythonpath forwarding")

    assert capture["inv"].pythonpath == ("src",)


def test_inactive_spike_policy_emits_sandbox_inactive_event(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inactive_policy = types.SimpleNamespace(active=False, note="unsandboxed_by_config")
    monkeypatch.setattr(
        "imp.agents.subagents._build_isolated_policy", lambda *_a, **_kw: inactive_policy)

    quest = _make_quest(tmp_path)
    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}]))
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


def test_isolated_dispatch_without_quest_context_raises(tmp_path: Path) -> None:
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    ctx.subagent_defs = {"prober": _isolated_agent("prober")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude"
    ctx.parent_transcript_dir = tmp_path / "steps" / "p" / "s"
    ctx.coordinator = InterruptCoordinator()
    ctx.quest = None                                     # nested-dispatch shape
    ctx.quest_context = QuestContext(
        id="x", title="X", project_root=tmp_path, workflow_dir=tmp_path / "wf")
    ctx.spawn_subagent = _spawn([{"type": "agent_result", "payload": {"text": "ok"}}])

    with pytest.raises(RuntimeError, match="isolated_worktree"):
        anyio.run(dispatch_task, ctx, "prober", "go")


def test_build_isolated_policy_passes_disposable_tree_true(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import imp.agents.subagents as subagents
    from imp.engine.sandbox import SandboxConfig

    captured: dict = {}

    def _fake_resolve_policy(**kwargs):
        captured.update(kwargs)
        return "sentinel-policy"

    monkeypatch.setattr("imp.engine.sandbox.resolve_policy", _fake_resolve_policy)
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    # A REAL SandboxConfig so _build_isolated_policy reaches the patched
    # resolve_policy (a SimpleNamespace cfg would break the resolve_policy call).
    monkeypatch.setattr(
        "imp.engine.sandbox.sandbox_config_from_raw",
        lambda raw: SandboxConfig(),
    )
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(config=SimpleNamespace(sandbox={}, pythonpath=())),
    )
    monkeypatch.setattr(
        "imp.engine.env.engine_env", lambda **kw: {})
    monkeypatch.setattr(
        "imp.engine.env.overlay_engine_env", lambda parent, overlay: {})

    quest = SimpleNamespace(
        id="abc", title="T", project_root=tmp_path, control_root=tmp_path,
        quest_dir=tmp_path / "change", base_branch="master",
    )
    result = subagents._build_isolated_policy(quest, tmp_path / "wt", test_gates=None)

    assert result == "sentinel-policy"
    assert captured["disposable_tree"] is True


def test_build_isolated_policy_does_not_force_network(monkeypatch, tmp_path):
    # AC 11 (unit): the deleted network force no longer overrides run-level config.
    # With sandbox.network=False configured, the cfg handed to resolve_policy is
    # network=False (before this change it was forced True).
    from types import SimpleNamespace

    import imp.agents.subagents as subagents

    captured: dict = {}

    def _fake_resolve_policy(**kwargs):
        captured.update(kwargs)
        return "sentinel-policy"

    monkeypatch.setattr("imp.engine.sandbox.resolve_policy", _fake_resolve_policy)
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(
            config=SimpleNamespace(sandbox={"network": False}, pythonpath=())),
    )
    monkeypatch.setattr("imp.engine.env.engine_env", lambda **kw: {})
    monkeypatch.setattr(
        "imp.engine.env.overlay_engine_env", lambda parent, overlay: {})

    quest = SimpleNamespace(
        id="abc", title="T", project_root=tmp_path, control_root=tmp_path,
        quest_dir=tmp_path / "change", base_branch="master",
    )
    subagents._build_isolated_policy(quest, tmp_path / "wt", test_gates=None)

    assert captured["cfg"].network is False


def test_build_isolated_policy_threads_pythonpath(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import imp.agents.subagents as subagents
    from imp.engine.sandbox import SandboxConfig

    captured: dict = {}

    def _fake_resolve_policy(**kwargs):
        captured.update(kwargs)
        return "sentinel-policy"

    monkeypatch.setattr("imp.engine.sandbox.resolve_policy", _fake_resolve_policy)
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr(
        "imp.engine.sandbox.sandbox_config_from_raw", lambda raw: SandboxConfig())
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(
            config=SimpleNamespace(sandbox={}, pythonpath=("src",))))

    quest = SimpleNamespace(
        id="abc", title="T", project_root=tmp_path, control_root=tmp_path,
        quest_dir=tmp_path / "change", base_branch="master",
    )
    worktree = tmp_path / "wt"
    subagents._build_isolated_policy(quest, worktree, test_gates=None)

    # pythonpath resolved against the SPIKE worktree path, not quest.project_root.
    assert captured["engine_env_result"]["PYTHONPATH"].startswith(
        str(worktree / "src"))


def test_attach_isolated_home_overlay_siblings_spike(tmp_path, monkeypatch):
    # AC 1/6 (unit): overlay dirs are SIBLINGS of the spike worktree (keyed by its
    # name), created on disk, and the cleanup removes them.
    import imp.agents.subagents as subagents
    from imp.engine.sandbox import HomeLayer
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: True)
    spike = tmp_path / ".imp" / "worktrees" / "spike-x-1"
    spike.mkdir(parents=True)
    pol, cleanup = subagents._attach_isolated_home(
        _active_home_policy(), spike, tmp_path / "q")
    assert isinstance(pol.home_layer, HomeLayer) and pol.home_layer.is_overlay
    upper = spike.parent / "spike-x-1.overlay-upper"
    assert upper.is_dir() and upper.parent == spike.parent and upper != spike
    assert pol.home_layer.upper == str(upper)
    assert cleanup is not None
    cleanup()
    assert not upper.exists()


def test_attach_isolated_home_falls_back_to_rw(tmp_path, monkeypatch):
    # AC 8 (unit): no overlay support → rw-bind fallback, no overlay dirs created.
    import imp.agents.subagents as subagents
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: False)
    spike = tmp_path / ".imp" / "worktrees" / "spike-x-1"
    spike.mkdir(parents=True)
    pol, cleanup = subagents._attach_isolated_home(
        _active_home_policy(), spike, tmp_path / "q")
    assert pol.home_layer is not None and not pol.home_layer.is_overlay
    assert pol.home_layer.home == "/home/u"
    assert not (spike.parent / "spike-x-1.overlay-upper").exists()
    cleanup()  # no-op, must not raise


def test_attach_isolated_home_noop_for_inactive(tmp_path, monkeypatch):
    import imp.agents.subagents as subagents
    from imp.engine.sandbox import SandboxPolicy
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: True)
    inactive = SandboxPolicy(
        active=False, worktree="", git_common_dir="", git_ro_remask=(),
        writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True)
    spike = tmp_path / ".imp" / "worktrees" / "spike-x-1"
    pol, cleanup = subagents._attach_isolated_home(inactive, spike, tmp_path / "q")
    assert pol is inactive and pol.home_layer is None
    assert cleanup is None


def test_attach_isolated_home_threads_resolved_home(tmp_path, monkeypatch):
    import imp.agents.subagents as subagents
    seen: dict = {}

    def capture(base, home):
        seen["base"], seen["home"] = base, home
        return False

    monkeypatch.setattr("imp.engine.sandbox.overlay_available", capture)
    spike = tmp_path / ".imp" / "worktrees" / "spike-x-1"
    spike.mkdir(parents=True)
    subagents._attach_isolated_home(_active_home_policy(), spike, tmp_path / "q")
    assert seen["base"] == tmp_path / "q"
    assert seen["home"] == Path("/home/u")


def test_isolated_dispatch_overlay_home_torn_down_with_worktree(
        tmp_path, monkeypatch):
    # AC 1/6 (wiring): dispatch attaches an overlay home keyed to the spike
    # worktree; the outer finally removes the overlay dirs with the worktree.
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: True)
    quest = _make_quest(tmp_path)
    capture: dict = {}
    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}],
                            capture=capture))

    anyio.run(dispatch_task, ctx, "prober", "measure it")

    pol = capture["inv"].sandbox_policy
    assert pol.home_layer is not None and pol.home_layer.is_overlay
    # resolve() to match create_spike_worktree's resolved return (macOS /tmp symlink)
    worktrees = (tmp_path / ".imp" / "worktrees").resolve()
    assert pol.home_layer.upper == str(worktrees / "spike-prb001-1.overlay-upper")
    # finally teardown removed both the overlay dirs and the spike worktree
    assert not (worktrees / "spike-prb001-1.overlay-upper").exists()
    assert not (worktrees / "spike-prb001-1").exists()


def test_isolated_dispatch_honors_network_off(tmp_path, monkeypatch):
    # AC 11: with sandbox.network=False configured, the isolated agent's policy has
    # network off (the deleted force-on no longer overrides run-level config) and its
    # bash argv carries --unshare-net.
    from types import SimpleNamespace

    from imp.engine.sandbox import build_sandbox_argv

    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: False)
    # _make_quest uses load_effective_config internally; patch it AFTER so
    # Quest.create gets the real config object (which has tier_names() etc.).
    quest = _make_quest(tmp_path)
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(
            config=SimpleNamespace(sandbox={"network": False}, pythonpath=(),
                                   default_workflow="default")),
    )
    capture: dict = {}
    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}],
                            capture=capture))

    anyio.run(dispatch_task, ctx, "prober", "measure it")

    pol = capture["inv"].sandbox_policy
    assert pol.network is False
    argv = build_sandbox_argv("curl example.com", cwd=pol.worktree, policy=pol,
                              engine_env={})
    assert "--unshare-net" in argv


def test_isolated_dispatch_forks_spike_from_base_changed_on_disk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.overlay_available", lambda _b, _h: False)
    _seed_control(tmp_path)
    # A pre-go-live draft: this is the window in which a spiker runs.
    quest = Quest.create_draft(tmp_path, title="T", quest_id="prb900",
                               base_branch="main")
    release_tip = _branch_with_commit(tmp_path, "release-2.0")
    # `imp edit --base` runs in the daemon against its own Quest.load; `quest` stays stale.
    Quest.load(tmp_path, "prb900").set_base_branch("release-2.0")
    assert quest.base_branch == "main"

    seen: dict = {}

    def _record(inv) -> None:
        # Captured inside the runner: teardown removes the spike tree on the way out.
        seen["head"] = _git(
            inv.quest_context.project_root, "rev-parse", "HEAD").stdout.strip()

    ctx = _ctx(tmp_path, quest, name="prober", agent=_isolated_agent("prober"),
               spawn=_spawn([{"type": "agent_result", "payload": {"text": "ok"}}],
                            on_run=_record))

    assert anyio.run(dispatch_task, ctx, "prober", "spike it") == "ok"
    assert seen["head"] == release_tip
