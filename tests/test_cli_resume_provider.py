from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.providers import BUILTIN_FLOOR
from imp.providers.config import ProviderModel
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _pin_of(quest: Quest) -> object:
    return json.loads((quest.quest_dir / "quest.json").read_text())["provider"]


def _seed(tmp_path: Path, *, live: bool = False,
          resolved: str = "claude") -> Quest:
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="demo", quest_id="feat",
                         base_branch="main")
    state = State.load(quest.state_dir)
    state.status = "paused"
    state.has_started = True
    state.resolved_tiers = {
        t: ProviderModel(provider=resolved, model=mo)
        for t, mo in BUILTIN_FLOOR[resolved].tier_defaults.items()}
    state.save(quest.state_dir)
    if live:
        (quest.quest_dir / "lock").write_text(str(os.getpid()))
    return quest


def _stub_headless(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (True, False))
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit",
                        lambda *a, **k: None)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus",
                        lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())

    async def _fake(quest: Quest, **kw: Any) -> int:
        seen.append(kw)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _fake)
    return seen


def _ns(**over: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        token="feat", non_interactive=True, message="go", autonomous=False,
        provider=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_the_resume_parser_accepts_the_provider_flag() -> None:
    args = m._build_parser().parse_args(["resume", "feat", "--provider", "codex"])
    assert args.provider == "codex"
    assert m._build_parser().parse_args(["resume", "feat"]).provider is None


def test_an_unknown_provider_exits_two_before_touching_the_quest(
    tmp_path, monkeypatch, capsys, floor_availability,
) -> None:
    quest = _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stub_headless(monkeypatch)
    floor_availability({"claude", "codex"})

    rc = anyio.run(m._async_cmd_resume, _ns(provider="bogus"))

    assert rc == 2
    assert capsys.readouterr().err.strip() == (
        "imp: --provider: unknown provider 'bogus' "
        "(available here: claude, codex; needs setup: anthropic, openai)")
    assert _pin_of(quest) is None


def test_a_stopped_quest_gets_the_pin_written_before_attach(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    quest = _seed(tmp_path, live=False)
    monkeypatch.chdir(tmp_path)
    seen = _stub_headless(monkeypatch)
    floor_availability({"claude", "codex"})

    rc = anyio.run(m._async_cmd_resume, _ns(provider="codex"))

    assert rc == 0
    assert _pin_of(quest) == "codex"          # the spawned daemon reads it at (a)
    assert seen[0]["provider"] is None        # no frame: nothing is listening


def test_a_live_quest_gets_a_frame_and_no_disk_write(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    # A write behind a running engine would be durable and completely inert:
    # no reconcile and no wake predicate reads quest.json again.
    quest = _seed(tmp_path, live=True)
    monkeypatch.chdir(tmp_path)
    seen = _stub_headless(monkeypatch)
    floor_availability({"claude", "codex"})

    rc = anyio.run(m._async_cmd_resume, _ns(provider="codex"))

    assert rc == 0
    assert _pin_of(quest) is None
    assert seen[0]["provider"] == "codex"


def test_naming_the_vendor_already_resolved_changes_nothing(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    quest = _seed(tmp_path, live=True, resolved="codex")
    monkeypatch.chdir(tmp_path)
    seen = _stub_headless(monkeypatch)
    floor_availability({"claude", "codex"})

    rc = anyio.run(m._async_cmd_resume, _ns(provider="codex"))

    assert rc == 0
    assert _pin_of(quest) is None
    assert seen[0]["provider"] is None


def test_a_non_resumable_status_leaves_no_mutated_quest_json(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    # Validate the name early, write it late: a rejected resume must not pin.
    quest = _seed(tmp_path)
    state = State.load(quest.state_dir)
    state.status = "done"
    state.save(quest.state_dir)
    monkeypatch.chdir(tmp_path)
    _stub_headless(monkeypatch)
    floor_availability({"claude", "codex"})

    rc = anyio.run(m._async_cmd_resume, _ns(provider="codex"))

    assert rc == 2
    assert _pin_of(quest) is None


def test_a_never_started_seed_draft_still_takes_the_pin(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    # This path returns at _resume_opens_fresh, ahead of the liveness check.
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="demo", quest_id="seed1", base_branch="main",
        seed_opening="do the thing")
    monkeypatch.chdir(tmp_path)
    _stub_headless(monkeypatch)
    floor_availability({"claude", "codex"})
    fresh: list[Quest] = []

    async def _fake_fresh(quest: Quest, *, args: Any) -> int:
        fresh.append(quest)
        return 0

    monkeypatch.setattr(m, "_run_fresh_session", _fake_fresh)

    rc = anyio.run(m._async_cmd_resume, _ns(token="seed1", provider="codex"))

    assert rc == 0
    assert fresh and _pin_of(quest) == "codex"


def test_the_inert_path_carries_the_frame_too(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    quest = _seed(tmp_path, live=True)
    state = State.load(quest.state_dir)
    state.halt_condition = "waiting"
    state.save(quest.state_dir)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (False, False))
    monkeypatch.setattr("imp.cli.inert_resume.bare_turn_end_question",
                        lambda quest: "Pending Q?")
    floor_availability({"claude", "codex"})
    seen: dict[str, Any] = {}

    async def _fake_inert(quest: Quest, question: str, *, marker: str,
                          notice: str | None, autonomous: bool = False,
                          provider: str | None = None) -> int:
        seen["provider"] = provider
        return 0

    monkeypatch.setattr(m, "_resume_inert", _fake_inert)

    rc = anyio.run(m._async_cmd_resume,
                   _ns(provider="codex", non_interactive=False, message=None))

    assert rc == 0
    assert seen["provider"] == "codex"


def test_the_interactive_path_threads_the_frame(
    tmp_path, monkeypatch, floor_availability,
) -> None:
    _seed(tmp_path, live=True)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude", "codex"})
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr(m, "_select_headless", lambda args: (False, False))
    monkeypatch.setattr(m, "_print_splash_banner", lambda **k: False)
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.live_status.LiveStatus",
                        lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.session_run.make_interactive_solicit",
                        lambda *a, **k: None)
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    seen: list[dict[str, Any]] = []

    async def _fake_rsc(quest: Quest, **kw: Any) -> int:
        seen.append(kw)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _fake_rsc)

    async def _fake_clear_loop(control_root, base, *, first):
        return await first({})

    monkeypatch.setattr(m, "_clear_loop", _fake_clear_loop)

    # A --message skips the bare-turn-end inert branch, so this lands on the
    # interactive _first call site.
    rc = anyio.run(m._async_cmd_resume, _ns(provider="codex", non_interactive=False))

    assert rc == 0
    assert seen[0]["provider"] == "codex"
