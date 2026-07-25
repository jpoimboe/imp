from __future__ import annotations

from imp.agents.interactive import InteractiveSuccess
from imp.assets import bundled_workflow_dir


def test_build_init_kickoff_embeds_workflows_and_base() -> None:
    from imp.cli.init import _build_init_kickoff
    msg = _build_init_kickoff(
        workflows=[("default", "full pipeline"), ("minimal", "front-door -> review")],
        base_hint="develop", git_exists=False)
    assert "minimal — front-door -> review" in msg
    assert "develop" in msg
    assert "NOT yet a git repository" in msg


def test_build_init_kickoff_notes_existing_repo_and_missing_base() -> None:
    from imp.cli.init import _build_init_kickoff
    msg = _build_init_kickoff(
        workflows=[("default", "d")], base_hint=None, git_exists=True)
    assert "already a git repository" in msg
    assert "No default integration branch" in msg


def test_workflow_choices_include_bundled_minimal(tmp_path) -> None:
    from imp.cli.init import _workflow_choices
    choices = dict(_workflow_choices(tmp_path))
    assert "default" in choices and "minimal" in choices
    assert choices["minimal"].strip()   # non-empty description read from workflow.yaml


def test_run_init_conversation_loads_agent_and_passes_kickoff(tmp_path, monkeypatch):
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
        project_root=tmp_path, workflow_dir=bundled_workflow_dir("default"),
        provider_name="anthropic", model_name="m",
        allow_unsandboxed=True, color=False, output_fn=lambda _m: None)

    assert isinstance(result, InteractiveSuccess)
    assert captured["agent_def"].name == "init-conversation"      # real file loaded
    assert captured["provider_name"] == "anthropic"
    assert "minimal" in captured["initial_message"]               # workflow context embedded


def test_workflow_choices_exclude_internal_memory(tmp_path) -> None:
    from imp.cli.init import _build_init_kickoff, _workflow_choices
    choices = _workflow_choices(tmp_path)
    names = [name for name, _desc in choices]
    assert "memory" not in names
    assert {"default", "minimal"} <= set(names)
    # descriptions still come from each workflow.yaml
    assert all(desc.strip() for _name, desc in choices)
    kickoff = _build_init_kickoff(
        workflows=choices, base_hint=None, git_exists=True)
    assert "memory" not in kickoff
    assert "minimal" in kickoff
