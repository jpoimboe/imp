"""imp new --var: per-quest workflow variables at the CLI boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import anyio
import pytest

from imp.cli import main as cli_main
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control, _seed_control_with_vars


def _ns(**over: object) -> argparse.Namespace:
    """The headless `imp new` argument shape; `run_session_client` is stubbed out
    so the draft is created and nothing runs."""
    base: dict[str, object] = dict(
        idea=None, opening=None, title=None, depends_on=[], non_interactive=True,
        message="go", autonomous=False, workflow=None, base=None, var=None,
        background=False)
    base.update(over)
    return argparse.Namespace(**base)


def _stub_session(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    captured: dict[str, str] = {}

    async def _fake(quest: Quest, **kwargs: object) -> int:
        captured["quest_id"] = quest.id
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _fake)
    return captured


def _state_vars(control: Path, quest_id: str) -> dict[str, object]:
    return State.load(project_state_dir(control) / "quests" / quest_id / "state").vars


def test_var_seeds_this_quest_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured = _stub_session(monkeypatch)

    assert anyio.run(cli_main._async_cmd_new, _ns(var=["retries=5"])) == 0
    with_var = captured["quest_id"]
    assert anyio.run(cli_main._async_cmd_new, _ns()) == 0
    without_var = captured["quest_id"]

    assert with_var != without_var
    assert _state_vars(tmp_path, with_var)["retries"] == 5
    assert _state_vars(tmp_path, without_var)["retries"] == 3   # workflow default


def test_var_is_repeatable_and_types_each_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured = _stub_session(monkeypatch)

    rc = anyio.run(cli_main._async_cmd_new,
                   _ns(var=["jurisdiction=us", "retries=5", "strict=true"]))

    assert rc == 0
    assert _state_vars(tmp_path, captured["quest_id"]) == {
        "jurisdiction": "us", "retries": 5, "strict": True}


@pytest.mark.parametrize(("flags", "expected"), [
    (["jurisdiction"], "imp: --var expects NAME=VALUE, got 'jurisdiction'"),
    (["strict=yes"],
     "imp: variable 'strict' expects bool, got 'yes'; use true or false"),
    (["retries=many"], "imp: variable 'retries' expects int, got 'many'"),
    (["foo=1"], "imp: unknown variable 'foo' for workflow 'imp'; "
                "declared variables: jurisdiction, retries, strict"),
])
def test_a_rejected_var_errors_and_creates_no_quest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], flags: list[str], expected: str,
) -> None:
    _seed_control_with_vars(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stub_session(monkeypatch)

    rc = anyio.run(cli_main._async_cmd_new, _ns(var=flags))

    assert rc == 2
    assert capsys.readouterr().err.strip() == expected
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_unknown_var_for_a_workflow_that_declares_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stub_session(monkeypatch)

    rc = anyio.run(cli_main._async_cmd_new, _ns(var=["foo=1"]))

    assert rc == 2
    assert capsys.readouterr().err.strip() == (
        "imp: unknown variable 'foo' for workflow 'imp'; it declares no variables")
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_backlog_path_persists_the_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = anyio.run(cli_main._async_cmd_new, _ns(
        idea="An idea", non_interactive=False, message=None, var=["retries=5"]))

    assert rc == 0
    (quest_dir,) = (project_state_dir(tmp_path) / "quests").iterdir()
    assert json.loads((quest_dir / "quest.json").read_text())["vars"] == {"retries": 5}


def test_backlog_path_rejects_a_bad_var_without_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The third creation path with its own resolve/render pair: it too must fail
    # before the id is minted.
    _seed_control_with_vars(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = anyio.run(cli_main._async_cmd_new, _ns(
        idea="An idea", non_interactive=False, message=None, var=["retries=many"]))

    assert rc == 2
    assert capsys.readouterr().err.strip() == (
        "imp: variable 'retries' expects int, got 'many'")
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_background_flag_threads_the_overrides_through_apply_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `imp new --background --var` is the one creation hop that leaves
    # _async_cmd_new for apply_new; drive it from the argv shape, not apply_new.
    _seed_control_with_vars(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "imp.remote.daemon.spawn.start_quest", lambda control, quest_id: None)

    rc = anyio.run(cli_main._async_cmd_new,
                   _ns(background=True, var=["retries=5"]))

    assert rc == 0
    (quest_dir,) = (project_state_dir(tmp_path) / "quests").iterdir()
    assert json.loads((quest_dir / "quest.json").read_text())["vars"] == {"retries": 5}


def test_background_path_persists_the_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    monkeypatch.setattr(
        "imp.remote.daemon.spawn.start_quest", lambda control, quest_id: None)

    rc, message = cli_main.apply_new(
        tmp_path, opening="go", title=None, workflow=None, base=None, idea=None,
        depends_on=[], var_flags=["retries=5"], background=True)

    assert rc == 0, message
    (quest_dir,) = (project_state_dir(tmp_path) / "quests").iterdir()
    assert json.loads((quest_dir / "quest.json").read_text())["vars"] == {"retries": 5}


def test_background_path_rejects_a_bad_var_without_creating(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)

    rc, message = cli_main.apply_new(
        tmp_path, opening="go", title=None, workflow=None, base=None, idea=None,
        depends_on=[], var_flags=["retries=many"], background=True)

    assert rc == 2
    assert message == "imp: variable 'retries' expects int, got 'many'"
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_parser_collects_repeated_var_flags_into_a_list() -> None:
    # action="append" is load-bearing: without it argparse hands back a bare str
    # and resolve_var_flags would iterate its characters.
    args = cli_main._build_parser().parse_args(
        ["new", "do a thing", "--var", "a=1", "--var", "b=2"])
    assert args.var == ["a=1", "b=2"]


def test_parser_var_defaults_to_none() -> None:
    args = cli_main._build_parser().parse_args(["new", "do a thing"])
    assert args.var is None


def test_session_proxy_payload_carries_the_raw_var_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inside a session `imp new` is proxied to the daemon: the raw strings must
    # ride along or the flag would be silently dropped.
    monkeypatch.setenv("IMP_SESSION_SOCKET", str(tmp_path / "sock"))
    sent: dict[str, object] = {}

    async def _fake_proxy(op: str, args: dict[str, object]) -> dict[str, object]:
        sent.update(args)
        return {"returncode": 0, "message": "imp: recorded backlog quest q1"}

    monkeypatch.setattr("imp.cli.mutation_proxy.proxy_mutation_async", _fake_proxy)

    rc = anyio.run(cli_main._async_cmd_new, _ns(var=["retries=5"]))

    assert rc == 0
    assert sent["var_flags"] == ["retries=5"]
