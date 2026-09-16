from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from imp import __version__
from imp.cli.main import _build_parser, _command_names, _top_level_help, main
from tests.conftest import _command_summaries

_COLOR_ENV = ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE", "IMP_THEME")

_FRONT_DOOR = ([], ["help"], ["-h"], ["--help"])


def _front_door(argv: list[str], cwd: Path,
                env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Strip the ambient color/theme vars: NO_COLOR disables on mere presence.
    base = {k: v for k, v in os.environ.items() if k not in _COLOR_ENV}
    return subprocess.run([sys.executable, "-m", "imp", *argv], cwd=cwd,
                          capture_output=True, text=True, check=False,
                          env={**base, "COLUMNS": "80", **env})


def _rows(text: str) -> set[str]:
    return {line.split()[0] for line in text.splitlines()
            if line.startswith("    ") and not line.startswith("     ")}


def test_top_level_help_lists_every_command_and_the_new_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    text = _top_level_help(_build_parser())
    for name in ("chat", "new", "resume", "show", "log", "abandon", "config"):
        assert name in _rows(text)
    assert "imp init" in text
    assert "Run 'imp <command> -h' for a command's options.\n" in text


def test_top_level_help_resume_row_is_accurate() -> None:
    # resume must NOT claim it resumes "the most recent" quest.
    assert "most recent" not in _top_level_help(_build_parser()).lower()


@pytest.mark.parametrize("argv", _FRONT_DOOR)
def test_front_door_prints_the_generated_help_rc0(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("usage: imp [-h] [--version] <command> ...\n")
    assert "\nimp — multi-agent software development workflow\n" in out
    assert out.endswith("New to a project? Run 'imp init' to get started.\n"
                        "Run 'imp <command> -h' for a command's options.\n")
    assert _rows(out) == _command_names(_build_parser())
    assert "error:" not in out.lower()


def test_tui_flag_removed_from_new_chat_resume() -> None:
    parser = _build_parser()
    for cmd in ("new", "chat", "resume"):
        with pytest.raises(SystemExit):
            parser.parse_args([cmd, "--tui"])


def test_version_flag_prints_version_rc0(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "imp", "--version"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == f"imp {__version__}"


def test_imp_help_command_equals_command_dash_h(tmp_path: Path) -> None:
    helped = subprocess.run(
        [sys.executable, "-m", "imp", "help", "resume"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    direct = subprocess.run(
        [sys.executable, "-m", "imp", "resume", "-h"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert helped.returncode == 0
    assert direct.returncode == 0
    assert helped.stdout == direct.stdout


def test_help_is_a_registered_subcommand_with_an_optional_command_arg() -> None:
    parser = _build_parser()
    assert "help" in _command_names(parser)
    assert parser.parse_args(["help", "resume"]).command == "resume"
    assert parser.parse_args(["help"]).command is None


def test_imp_help_help_prints_the_help_subparsers_own_usage(tmp_path: Path) -> None:
    r = _front_door(["help", "help"], tmp_path, {"NO_COLOR": "1"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage: imp help [-h] [<command>]\n")
    assert "  <command>\n" in r.stdout


def test_front_door_four_ways_are_byte_identical_piped(tmp_path: Path) -> None:
    runs = [_front_door(argv, tmp_path, {"NO_COLOR": "1"}) for argv in _FRONT_DOOR]
    for r in runs:
        assert r.returncode == 0, r.stderr
    assert len({r.stdout for r in runs}) == 1, "front door forms disagree"
    assert runs[0].stdout.startswith("usage: imp [-h] [--version] <command> ...\n")


def test_front_door_four_ways_honor_imp_theme(tmp_path: Path) -> None:
    env = {"FORCE_COLOR": "1", "IMP_THEME": "dark"}
    runs = [_front_door(argv, tmp_path, env) for argv in _FRONT_DOOR]
    assert len({r.stdout for r in runs}) == 1, "front door forms disagree"
    for r in runs:
        assert r.returncode == 0, r.stderr
        assert "\x1b[38;2;255;154;60m" in r.stdout


def test_front_door_never_reads_the_project_config_theme(tmp_path: Path) -> None:
    """A project config naming `dark` must not reach the front door: it resolves
    IMP_THEME or the universal `ansi`, never load_effective_config."""
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("theme: dark\n")
    env = {"FORCE_COLOR": "1"}
    runs = [_front_door(argv, tmp_path, env) for argv in _FRONT_DOOR]
    assert len({r.stdout for r in runs}) == 1, "front door forms disagree"
    for r in runs:
        assert r.returncode == 0, r.stderr
        assert "\x1b[33m" in r.stdout
        assert "\x1b[38;2;" not in r.stdout


def test_the_entrypoint_dispatches_without_touching_the_legacy_config(
    tmp_path: Path,
) -> None:
    """Nothing runs ahead of command dispatch: a config file at the pre-IMP_HOME
    location stays where it is, and $IMP_HOME/config.yaml is never minted."""
    legacy = tmp_path / "xdg" / "imp" / "config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("theme: dark\n")
    home = tmp_path / "home"
    env = {"IMP_HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
           "NO_COLOR": "1"}
    for argv, needle in ((["--help"], "usage: imp"), (["list"], "No quests yet")):
        r = _front_door(argv, tmp_path, env)
        assert r.returncode == 0, r.stderr
        assert needle in r.stdout
        assert "imp: moved" not in r.stderr
        assert legacy.read_text() == "theme: dark\n"
        assert not (home / "config.yaml").exists()


@pytest.mark.parametrize("argv", [["help"], ["help", "resume"], ["help", "frobnicate"],
                                  [], ["-h"], ["--help"]])
def test_no_help_form_falls_through_to_parse_args(argv: list[str], capsys) -> None:
    """_dispatch_meta must settle every help form itself (or exit inside argparse),
    never return None — main() resolves the config-backed theme after parse_args."""
    import imp.cli.main as m

    try:
        rc = m._dispatch_meta(argv, m._build_parser())
    except SystemExit as exc:          # `help <cmd>` exits inside argparse
        rc = exc.code
    capsys.readouterr()
    assert isinstance(rc, int), f"{argv} fell through to parse_args"


def test_imp_help_unknown_command_rc2(capsys) -> None:
    rc = main(["help", "frobnicate"])
    err = capsys.readouterr().err
    assert rc == 2
    assert err == "imp: unknown command 'frobnicate'. Run 'imp help'.\n"
    assert "invalid choice" not in err.lower()


def test_unknown_command_friendly_rc2(capsys) -> None:
    rc = main(["frobnicate"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "imp: unknown command 'frobnicate'. Run 'imp help'." in err
    # Must NOT fall through to argparse's raw invalid-choice list.
    assert "invalid choice" not in err.lower()


def test_known_command_still_dispatches(tmp_path, monkeypatch) -> None:
    # A real command is not swallowed by the unknown-command guard.
    monkeypatch.chdir(tmp_path)
    rc = main(["list"])
    assert rc == 0


def test_help_rows_match_the_live_subcommands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift guard, by set equality: no command may be missing from the help."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    parser = _build_parser()
    assert _rows(_top_level_help(parser)) == _command_names(parser)
    assert {"events", "search", "edit", "remote-control", "help"} <= _command_names(parser)


def test_new_summary_reads_as_an_opening_intent() -> None:
    summary = _command_summaries()["new"]
    assert "opening intent" in summary
    assert "title" not in summary


def test_main_swallows_broken_pipe_on_overview(monkeypatch) -> None:
    """The bare-`imp` overview path prints before the command dispatch, so it
    must also sit inside main()'s BrokenPipeError guard: a reader closing the
    pipe early returns the conventional 128+SIGPIPE status instead of escaping
    as an uncatchable traceback. os.open/os.dup2 are stubbed so the handler's
    stdout-fd redirect can't clobber pytest's own stdout."""
    import signal

    import imp.cli.main as m

    def boom(_parser: object) -> str:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(m, "_top_level_help", boom)
    monkeypatch.setattr(m.os, "open", lambda *a, **k: 0)
    monkeypatch.setattr(m.os, "dup2", lambda *a, **k: None)

    rc = main([])
    assert rc == 128 + signal.SIGPIPE


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_autonomous_flag_parses_on_new_chat_resume() -> None:
    p = _build_parser()
    assert p.parse_args(["new", "--autonomous"]).autonomous is True
    assert p.parse_args(["new"]).autonomous is False
    assert p.parse_args(["chat", "--autonomous"]).autonomous is True
    assert p.parse_args(["resume", "--autonomous"]).autonomous is True
    assert p.parse_args(["resume"]).autonomous is False


@pytest.mark.anyio
async def test_run_fresh_session_threads_autonomous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import imp.cli.main as main

    captured: dict[str, object] = {}

    async def _fake_rsc(quest: object, **kw: object) -> int:
        captured.update(kw)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _fake_rsc)
    args = main._build_parser().parse_args(["new", "--headless", "--autonomous"])
    rc = await main._run_fresh_session(
        SimpleNamespace(id="x", title="t", create_mode="new",
                        control_root=Path("/checkouts/imp")), args=args)
    assert rc == 0
    assert captured["autonomous"] is True


@pytest.mark.anyio
async def test_run_fresh_session_interactive_branch_threads_autonomous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import imp.cli.main as m

    captured: dict[str, object] = {}

    async def _fake_rsc(quest: object, **kw: object) -> int:
        captured.update(kw)
        return 0

    async def _fake_clear_loop(control: object, base: str, *, first: object) -> int:
        return await first({})

    monkeypatch.setattr(m, "_select_headless", lambda _a: (False, False))
    monkeypatch.setattr(m, "_clear_loop", _fake_clear_loop)
    monkeypatch.setattr(
        "imp.cli.session_run.make_interactive_solicit", lambda *a, **k: (lambda _: None)
    )
    monkeypatch.setattr("imp.cli.session_run.run_session_client", _fake_rsc)

    args = m._build_parser().parse_args(["new", "--autonomous"])
    rc = await m._run_fresh_session(
        SimpleNamespace(id="x", title="t", control_root=Path("/checkouts/imp"),
                        seed_opening=None, create_mode="new", base_branch="main"),
        args=args)
    assert rc == 0
    assert captured.get("autonomous") is True


@pytest.mark.parametrize("cmd", ["new", "chat", "resume"])
def test_headless_flag_replaces_non_interactive(cmd: str) -> None:
    # New spelling parses and still lands on the non_interactive dest.
    args = _build_parser().parse_args([cmd, "--headless"])
    assert args.non_interactive is True
    # Old spelling is gone: argparse rejects it.
    with pytest.raises(SystemExit):
        _build_parser().parse_args([cmd, "--non-interactive"])


def test_artifact_commit_subcommand_is_removed() -> None:
    # The nested-CLI artifact surface is gone — the engine journals artifacts
    # in-process at each step boundary. The subcommand must not be registered.
    assert "artifact-commit" not in _command_names(_build_parser())


@pytest.mark.parametrize("argv", _FRONT_DOOR)
def test_front_door_pulls_in_no_heavy_dependency(argv: list[str], tmp_path: Path) -> None:
    """Every front-door form must print without loading the agent stack.
    Fresh interpreter: in-process, the suite has already imported all of these."""
    code = (
        "import runpy, sys\n"
        f"sys.argv = ['imp', *{argv!r}]\n"
        "try:\n"
        "    runpy.run_module('imp', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "heavy = {'pydantic', 'jinja2', 'httpx', 'PIL', 'rich', 'yaml', 'ruamel',\n"
        "         'anyio', 'prompt_toolkit'}\n"
        "leaked = sorted({n.split('.')[0] for n in sys.modules} & heavy)\n"
        "sys.stderr.write('LEAKED:' + ','.join(leaked) + '\\n')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    marker = [ln for ln in proc.stderr.splitlines() if ln.startswith("LEAKED:")]
    assert marker, f"child did not report; stderr was {proc.stderr!r}"
    assert marker[0] == "LEAKED:", f"imp {' '.join(argv)} imported {marker[0][len('LEAKED:'):]}"


# yaml/ruamel are absent deliberately: a dispatching command parses config through ruamel.
_FORBIDDEN_TOP_LEVEL = frozenset({
    "httpx", "rich", "click", "pygments", "pydantic", "pydantic_core",
    "jinja2", "anyio", "PIL", "prompt_toolkit",
})
_PROVIDER_MODULES = frozenset({
    "imp.providers.anthropic", "imp.providers.openai",
    "imp.providers.claude", "imp.providers.codex",
    "imp.providers.codex_runner", "imp.providers.scripted",
})


@pytest.mark.parametrize(
    "cmd,witness",
    # A per-command witness module: an early bail-out cannot pass with an empty leak set.
    [("config", "imp.cli.config_cmd"), ("list", "imp.engine.replay")],
)
def test_read_only_command_dispatches_without_the_provider_stack(
    cmd: str, witness: str, tmp_path: Path
) -> None:
    """A dispatching read-only command must reach its handler without importing a
    provider module, and so without httpx and the agent stack behind it. The cwd
    must be a real work tree: `_cmd_list` bails out before touching the store when
    `find_control_root` raises, which would make this guard vacuous."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    code = (
        "import runpy, sys\n"
        f"sys.argv = ['imp', {cmd!r}]\n"
        "try:\n"
        "    runpy.run_module('imp', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        f"forbidden = {set(_FORBIDDEN_TOP_LEVEL)!r}\n"
        f"providers = {set(_PROVIDER_MODULES)!r}\n"
        "heavy = sorted({n.split('.')[0] for n in sys.modules} & forbidden)\n"
        "prov = sorted(providers & sys.modules.keys())\n"
        f"sys.stderr.write('WITNESS:' + str({witness!r} in sys.modules) + '\\n')\n"
        "sys.stderr.write('LEAKED:' + ','.join(heavy) + '\\n')\n"
        "sys.stderr.write('PROVIDERS:' + ','.join(prov) + '\\n')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    reported = {
        key: value
        for key, _, value in (ln.partition(":") for ln in proc.stderr.splitlines())
        if key in {"WITNESS", "LEAKED", "PROVIDERS"}
    }
    assert reported.keys() == {"WITNESS", "LEAKED", "PROVIDERS"}, (
        f"child did not report; stderr was {proc.stderr!r}"
    )
    assert reported["WITNESS"] == "True", (
        f"imp {cmd} never imported {witness} — it exited before dispatching, so"
        f" this guard would pass vacuously; stdout was {proc.stdout!r}"
    )
    assert reported["LEAKED"] == "", f"imp {cmd} imported {reported['LEAKED']}"
    assert reported["PROVIDERS"] == "", f"imp {cmd} imported {reported['PROVIDERS']}"


@pytest.mark.parametrize(
    "cmd,handler",
    [("init", "_cmd_init"), ("new", "_cmd_new"),
     ("chat", "_cmd_chat"), ("resume", "_cmd_resume")],
)
def test_long_lived_commands_warm_the_module_graph_first(
    cmd: str, handler: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The four long-lived commands import imp's whole graph before their
    handler runs, so a concurrent on-disk update can't splice into the run."""
    import imp.cli.main as m

    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    def _fake_warm() -> list[tuple[str, BaseException]]:
        calls.append("warm")
        return []

    def _fake_handler(_ns: object) -> int:
        calls.append("handler")
        return 0

    monkeypatch.setattr("imp.remote.session.warmup.warm_module_graph", _fake_warm)
    monkeypatch.setattr(m, handler, _fake_handler)

    assert main([cmd]) == 0
    assert calls == ["warm", "handler"]


def test_short_lived_command_is_not_warmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`imp list` renders and exits — paying warmup would be a visible
    regression. Its namespace carries no `warm` at all, as does the
    hand-built `events search` one, hence main()'s getattr default."""
    import imp.cli.main as m

    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    def _fake_warm() -> list[tuple[str, BaseException]]:
        calls.append("warm")
        return []

    def _fake_handler(_ns: object) -> int:
        calls.append("handler")
        return 0

    monkeypatch.setattr("imp.remote.session.warmup.warm_module_graph", _fake_warm)
    monkeypatch.setattr(m, "_cmd_list", _fake_handler)

    assert main(["list"]) == 0
    assert calls == ["handler"]
    assert not hasattr(_build_parser().parse_args(["list"]), "warm")
    assert not hasattr(m._build_events_search_parser().parse_args([]), "warm")
