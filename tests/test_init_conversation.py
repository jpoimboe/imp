from __future__ import annotations

import pytest

from imp.agents.interactive import InteractiveSuccess
from imp.assets import bundled_assets_dir
from imp.cli.init import BaseDetection, InitScreenState, _screen_state_from_detection
from imp.cli.pythonpath_detect import ImportRoots
from imp.cli.verify_detect import DetectedTestCommand


def _screen_state() -> InitScreenState:
    return _screen_state_from_detection(
        workflow="default",
        base=BaseDetection("master", "from origin/HEAD"),
        tests=DetectedTestCommand("make check", "from Makefile"),
        setup=None,
        roots=ImportRoots(["src"], "from pyproject.toml"))


def _run_capturing_init_conversation(tmp_path, monkeypatch):
    """Run _run_init_conversation against a fake conversation seam (no model
    call); return (result, the kwargs handed to run_interactive_agent_sync)."""
    import imp.cli.init as initmod

    captured: dict[str, object] = {}

    def _fake_sync(**kw):
        captured.update(kw)
        return InteractiveSuccess({"status": "Success"})

    class _FakePrompt:
        def __init__(self, **kw) -> None:
            pass

        async def ask_async(self, *a, **k) -> str:
            return ""

    monkeypatch.setattr(initmod, "run_interactive_agent_sync", _fake_sync)
    monkeypatch.setattr(initmod, "Prompt", _FakePrompt)
    result = initmod._run_init_conversation(
        project_root=tmp_path, state=_screen_state(),
        provider_name="anthropic", model_name="m",
        allow_unsandboxed=True, color=False, output_fn=lambda _m: None)
    return result, captured


def test_build_init_kickoff_reads_back_every_row_with_its_provenance() -> None:
    from imp.cli.init import _build_init_kickoff
    msg = _build_init_kickoff(
        workflows=[("default", "full pipeline"), ("minimal", "front-door -> review")],
        state=_screen_state())
    assert "minimal — front-door -> review" in msg
    assert "  workflow: default  (full pipeline — spec, plan, implement, review, merge)" in msg
    assert "  branch: master  (from origin/HEAD)" in msg
    assert "  tests: make check  (from Makefile)" in msg
    assert "  import root: src  (from pyproject.toml)" in msg
    assert "already a git repository" in msg


def test_build_init_kickoff_carries_an_edited_row_as_the_users_own() -> None:
    from dataclasses import replace

    from imp.cli.init import _build_init_kickoff
    edited = replace(_screen_state(), branch="trunk", branch_provenance="you set this")
    msg = _build_init_kickoff(workflows=[("default", "d")], state=edited)
    assert "  branch: trunk  (you set this)" in msg
    assert "master" not in msg


def test_build_init_kickoff_shows_an_unresolved_branch_as_none() -> None:
    from imp.cli.init import _build_init_kickoff
    unresolved = _screen_state_from_detection(
        workflow="default", base=None, tests=None, setup=None,
        roots=ImportRoots([], "not a Python project"))
    msg = _build_init_kickoff(workflows=[("default", "d")], state=unresolved)
    assert "  branch: (none)  (nothing detected)" in msg


def test_workflow_choices_include_bundled_minimal(tmp_path) -> None:
    from imp.cli.init import _workflow_choices
    choices = dict(_workflow_choices(tmp_path))
    assert "default" in choices and "minimal" in choices
    assert choices["minimal"].strip()   # non-empty description read from workflow.yaml


def test_run_init_conversation_loads_agent_and_passes_kickoff(tmp_path, monkeypatch):
    result, captured = _run_capturing_init_conversation(tmp_path, monkeypatch)

    assert isinstance(result, InteractiveSuccess)
    assert captured["agent_def"].name == "init"                   # real file loaded
    assert captured["provider_name"] == "anthropic"
    assert "minimal" in captured["initial_message"]               # workflow context embedded


def test_workflow_choices_exclude_internal_memory(tmp_path) -> None:
    from imp.cli.init import _build_init_kickoff, _workflow_choices
    choices = _workflow_choices(tmp_path)
    names = [name for name, _desc in choices]
    assert "memory" not in names
    assert {"default", "minimal", "patent"} <= set(names)
    # descriptions still come from each workflow.yaml
    assert all(desc.strip() for _name, desc in choices)
    kickoff = _build_init_kickoff(workflows=choices, state=_screen_state())
    assert "memory" not in kickoff
    assert "minimal" in kickoff


def test_run_init_conversation_pins_default_workflow_enum(tmp_path, monkeypatch):
    from imp.assets import selectable_workflow_names
    from imp.engine.loader import parse_agent_file

    _result, captured = _run_capturing_init_conversation(tmp_path, monkeypatch)
    schema = captured["agent_def"].output_schema
    original = parse_agent_file(bundled_assets_dir() / "init.md").output_schema

    # same fields, same order — the enum takes the original field's slot
    assert [f.name for f in schema.fields] == [f.name for f in original.fields]
    field = {f.name: f for f in schema.fields}["default_workflow"]
    assert field.type == "enum"
    assert field.values == tuple(selectable_workflow_names(tmp_path))
    assert field.required
    # every other field of THIS AgentDef's schema is untouched
    assert [f for f in schema.fields if f.name != "default_workflow"] == [
        f for f in original.fields if f.name != "default_workflow"]


def test_pinned_enum_bounces_offlist_workflow(tmp_path, monkeypatch):
    from imp.assets import selectable_workflow_names
    from imp.engine.completion import completion_instruction
    from imp.engine.errors import LoadError
    from imp.engine.schema import validate_agent_output

    _result, captured = _run_capturing_init_conversation(tmp_path, monkeypatch)
    schema = captured["agent_def"].output_schema
    payload = {"status": "Success", "git_init": False, "default_base": "main",
               "test_cmd_fast": "make check", "pythonpath": {"roots": ["src"]}}

    assert validate_agent_output(
        {**payload, "default_workflow": "minimal"}, schema) is None
    for bad in ("memory", "minimial"):
        with pytest.raises(LoadError, match="default_workflow"):
            validate_agent_output({**payload, "default_workflow": bad}, schema)

    expected = (
        "`default_workflow` ("
        + "|".join(selectable_workflow_names(tmp_path))
        + ")"
    )
    assert expected in completion_instruction(schema)


def test_run_init_resolves_no_workflow_dir() -> None:
    import inspect

    from imp.cli import init as initmod
    assert "resolve_workflow_dir" not in inspect.getsource(initmod.run_init)


def test_run_init_conversation_ignores_a_project_local_default_workflow(
    tmp_path, monkeypatch,
):
    """A committed imp/workflows/default/ used to shadow and break setup."""
    import imp.cli.init as initmod

    (tmp_path / "imp" / "workflows" / "default").mkdir(parents=True)

    captured: dict[str, object] = {}

    def _fake_sync(**kw):
        captured.update(kw)
        return InteractiveSuccess({"status": "Success"})

    class _FakePrompt:
        def __init__(self, **kw) -> None:
            pass

        async def ask_async(self, *a, **k) -> str:
            return ""

    monkeypatch.setattr(initmod, "run_interactive_agent_sync", _fake_sync)
    monkeypatch.setattr(initmod, "Prompt", _FakePrompt)

    result = initmod._run_init_conversation(
        project_root=tmp_path, state=_screen_state(),
        provider_name="anthropic", model_name="m",
        allow_unsandboxed=True, color=False, output_fn=lambda _m: None)

    assert isinstance(result, InteractiveSuccess)
    assert captured["agent_def"].name == "init"
    assert captured["asset_dir"] == bundled_assets_dir()


def test_build_init_kickoff_header_labels_the_lines_as_internal_step_names() -> None:
    from imp.cli.init import _build_init_kickoff
    msg = _build_init_kickoff(
        workflows=[("minimal", "front-door -> review")], state=_screen_state())
    assert "Available workflows (name — internal pipeline summary, in step names):" in msg
    assert "  minimal — front-door -> review" in msg
