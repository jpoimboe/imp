"""AC 5a: every leased-frontend writer holds the one WatchedStream for its entry point."""
from __future__ import annotations

import ast
import io
import pathlib

import imp.cli.main as m
from imp.cli.live_status import LiveStatus
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir


def _tree() -> ast.Module:
    return ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == name):
            return node
    raise AssertionError(f"{name} not found in main.py")


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and _callee(c) == name]


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _leased(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every module-level function that owns a leased attach, discovered rather than
    enumerated, so a sixth one cannot be added without this test seeing it."""
    return [n for n in tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            and _calls(n, "run_session_client")]


def _watched_name(fn: ast.AST) -> str:
    names = [
        target.id
        for node in ast.walk(fn) if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Call)
        and _callee(node.value) == "WatchedStream"
    ]
    assert len(names) == 1, f"expected exactly one WatchedStream binding, got {names}"
    return names[0]


def test_every_leased_writer_takes_the_same_wrapper_instance() -> None:
    tree = _tree()
    leased = _leased(tree)
    for fn in leased:
        watched = _watched_name(fn)
        for key in ("LiveStatus", "TerminalTitle"):
            for call in _calls(fn, key):
                arg = call.args[0]
                assert isinstance(arg, ast.Name) and arg.id == watched, (
                    f"{fn.name}: {key} takes {ast.unparse(arg)}, not {watched}")
        for key in ("LiveDriver", "_build_resume_driver", "run_session_client"):
            for call in _calls(fn, key):
                arg = _kw(call, "stream")
                assert isinstance(arg, ast.Name) and arg.id == watched, (
                    f"{fn.name}: {key} takes stream="
                    f"{'<missing>' if arg is None else ast.unparse(arg)}, "
                    f"not {watched}")
    attaches = sum(len(_calls(fn, "run_session_client")) for fn in leased)
    assert attaches >= 8, (
        f"expected at least the spec's eight leased entry points, saw {attaches}")


def test_read_only_log_renderers_keep_plain_stdout() -> None:
    tree = _tree()
    for fname in ("_follow_log", "_follow_live"):
        fn = _func(tree, fname)
        assert not _calls(fn, "WatchedStream")
        (status,) = _calls(fn, "LiveStatus")
        assert ast.unparse(status.args[0]) == "sys.stdout"
        (driver,) = _calls(fn, "LiveDriver")
        assert ast.unparse(_kw(driver, "stream")) == "sys.stdout"


def test_build_resume_driver_writes_to_the_stream_it_is_given(
    tmp_path: pathlib.Path,
) -> None:
    out = io.StringIO()
    quest = Quest(
        id="feat", title="T", base_branch="main", control_root=tmp_path,
        project_root=tmp_path,
        quest_dir=project_state_dir(tmp_path) / "quests" / "feat")
    state = State(id="feat", step="plan", step_attempt=0, status="paused")
    driver = m._build_resume_driver(
        quest=quest, state=state, status=LiveStatus(out), color=False, stream=out)
    driver.echo_submitted_reply("hi")
    assert "> hi" in out.getvalue()
