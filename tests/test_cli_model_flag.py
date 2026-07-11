from __future__ import annotations

import argparse

import pytest

import imp.cli.main as main
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_new_parser_accepts_model_flag() -> None:
    args = main._build_parser().parse_args(
        ["new", "do a thing", "--model", "anthropic:claude-fable-5"])
    assert args.model == "anthropic:claude-fable-5"


def test_new_parser_accepts_bare_model_flag() -> None:
    args = main._build_parser().parse_args(
        ["new", "do a thing", "--model", "claude-fable-5"])
    assert args.model == "claude-fable-5"


def test_chat_parser_accepts_model_flag() -> None:
    args = main._build_parser().parse_args(["chat", "--model", "anthropic:claude-fable-5"])
    assert args.model == "anthropic:claude-fable-5"


def test_new_parser_model_defaults_none() -> None:
    args = main._build_parser().parse_args(["new", "do a thing"])
    assert args.model is None


def test_chat_parser_model_defaults_none() -> None:
    args = main._build_parser().parse_args(["chat"])
    assert args.model is None


def test_new_parser_rejects_malformed_model_at_parse_time() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main._build_parser().parse_args(["new", "x", "--model", "anthropic:"])
    assert exc_info.value.code == 2


def test_chat_parser_rejects_malformed_model_at_parse_time() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main._build_parser().parse_args(["chat", "--model", ":bogus"])
    assert exc_info.value.code == 2


def _full_ns(**over: object) -> argparse.Namespace:
    base = dict(
        base=None, workflow=None, idea=None, title=None,
        non_interactive=True, autonomous=False, message=None,
        opening="do a thing", depends_on=None, model=None)
    base.update(over)
    return argparse.Namespace(**base)


@pytest.mark.anyio
async def test_async_cmd_new_rejects_unavailable_model_provider(
    tmp_path, monkeypatch, capsys,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "imp.providers.resolve.available_providers", lambda config: ["anthropic"])

    started: dict[str, bool] = {}

    async def _fake_rsc(change, **kw):
        started["ran"] = True
        return 0

    monkeypatch.setattr(main, "run_session_client", _fake_rsc)

    rc = await main._async_cmd_new(_full_ns(model="openai:gpt-5.5"))

    assert rc == 2
    assert "not available" in capsys.readouterr().err
    assert "ran" not in started  # aborted before the run started


@pytest.mark.anyio
async def test_async_cmd_new_threads_flag_model_to_run(
    tmp_path, monkeypatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "imp.providers.resolve.available_providers", lambda config: ["anthropic"])
    monkeypatch.setattr(main, "_select_headless", lambda args: (True, False))

    captured: dict[str, object] = {}

    async def _fake_rsc(change, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(main, "run_session_client", _fake_rsc)

    rc = await main._async_cmd_new(_full_ns(model="anthropic:claude-fable-5"))

    assert rc == 0
    assert captured["model"] == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_typed_opening_model_overrides_flag_model(
    tmp_path, monkeypatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "imp.providers.resolve.available_providers", lambda config: ["anthropic"])

    async def _fake_open(change, *, model_sink=None, **kw):
        if model_sink is not None:
            model_sink["model"] = "anthropic:claude-sonnet-4-6"  # typed /model
        return "brainstorm this"

    captured: dict[str, object] = {}

    async def _fake_rsc(change, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(main, "_read_opening_message", _fake_open)
    monkeypatch.setattr(main, "run_session_client", _fake_rsc)
    monkeypatch.setattr(main, "_should_prompt_opening", lambda **kw: True)
    monkeypatch.setattr(main, "_select_headless", lambda args: (False, False))

    rc = await main._async_cmd_new(
        _full_ns(model="anthropic:claude-fable-5", non_interactive=False, opening=None))

    assert rc == 0
    # the /model typed at the opening prompt wins over the --model flag
    assert captured["model"] == "anthropic:claude-sonnet-4-6"
