from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _write_pb(control: Path, name: str) -> None:
    d = control / "imp" / "playbooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\ndescription: d-{name}\n---\nbody\n", encoding="utf-8")


def test_run_parses_name_args_and_findings_only() -> None:
    ns = m._build_parser().parse_args(
        ["run", "dead-code", "src/only/", "--findings-only"])
    assert ns.name == "dead-code"
    assert ns.playbook_args == "src/only/"
    assert ns.findings_only is True


@pytest.mark.anyio
async def test_pick_playbook_single_auto_selects(monkeypatch, tmp_path: Path) -> None:
    from imp.cli.playbooks import Playbook
    monkeypatch.setattr(
        m.playbooks, "list_playbooks",
        lambda _: [Playbook(name="solo", description="d-solo", body="body")])
    assert await m._pick_playbook(tmp_path, non_interactive=False) == "solo"


@pytest.mark.anyio
async def test_pick_playbook_none_exits_2(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(m.playbooks, "list_playbooks", lambda _: [])
    rc = await m._pick_playbook(tmp_path, non_interactive=False)
    err = capsys.readouterr().err
    assert rc == 2
    assert "No playbooks found" in err


@pytest.mark.anyio
async def test_pick_playbook_multiple_non_interactive_lists_and_exits_2(
    tmp_path: Path, capsys,
) -> None:
    _write_pb(tmp_path, "aaa")
    _write_pb(tmp_path, "bbb")
    rc = await m._pick_playbook(tmp_path, non_interactive=True)
    err = capsys.readouterr().err
    assert rc == 2
    assert "aaa" in err and "bbb" in err
    assert "d-aaa" in err            # description rendered in the plain table


@pytest.mark.anyio
async def test_pick_playbook_non_tty_lists_and_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    _write_pb(tmp_path, "aaa")
    _write_pb(tmp_path, "bbb")
    monkeypatch.setattr(m, "_interactive", lambda: False)
    rc = await m._pick_playbook(tmp_path, non_interactive=False)
    err = capsys.readouterr().err
    assert rc == 2
    assert "aaa" in err and "bbb" in err


@pytest.mark.anyio
async def test_pick_playbook_interactive_uses_item_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    _write_pb(tmp_path, "aaa")
    _write_pb(tmp_path, "bbb")
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(items, *, render_item, header, color):
        return items[1]

    monkeypatch.setattr(m.resume_select, "run_item_picker", fake_picker)
    assert await m._pick_playbook(tmp_path, non_interactive=False) == "bbb"


@pytest.mark.anyio
async def test_run_unknown_name_exits_2(monkeypatch, tmp_path: Path, capsys) -> None:
    _write_pb(tmp_path, "aaa")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    async def boom_new(args):                # must never launch a change
        raise AssertionError("launched a change for an unknown playbook")

    monkeypatch.setattr(m, "_async_cmd_new", boom_new)
    ns = m._build_parser().parse_args(["run", "nope"])
    rc = await m._async_cmd_run(ns)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown playbook 'nope'" in err


@pytest.mark.anyio
async def test_run_known_name_delegates_to_new(monkeypatch, tmp_path: Path) -> None:
    _write_pb(tmp_path, "aaa")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    captured: dict[str, object] = {}

    async def fake_new(args):
        captured["playbook"] = args.playbook
        captured["opening"] = args.opening
        return 0

    monkeypatch.setattr(m, "_async_cmd_new", fake_new)
    ns = m._build_parser().parse_args(["run", "aaa", "the args"])
    rc = await m._async_cmd_run(ns)
    assert rc == 0
    assert captured == {"playbook": "aaa", "opening": "the args"}
