from __future__ import annotations

from pathlib import Path

from imp.agents.runner import QuestContext
from imp.agents.tools.context import ToolContext
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.assets import bundled_docs_dir
from imp.engine.state import JsonlEventLog
from imp.paths import project_state_dir


def _ctx(tmp_path: Path) -> ToolContext:
    wf = project_state_dir(tmp_path) / "quests" / "q" / "workflow"
    (wf / "skills").mkdir(parents=True)
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.quest_context = QuestContext(
        title="t", project_root=tmp_path, workflow_dir=wf,
    )
    ctx.event_log = JsonlEventLog(tmp_path / "events.jsonl")
    return ctx


def test_agent_loaded_skill_renders_docs_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    wf = ctx.quest_context.workflow_dir
    (wf / "skills" / "hub.md").write_text("Corpus at {{ docs.path }} — read it.\n")
    out = make_load_skill_tool().invoke(ctx, {"name": "hub"})
    assert "{{ docs.path }}" not in out
    assert out == f"Corpus at {bundled_docs_dir()} — read it.\n"


def test_user_skill_path_stays_raw(tmp_path: Path) -> None:
    from imp.agents.skills import load_user_skill

    skills = tmp_path / "skills"
    (skills / "user").mkdir(parents=True)
    (skills / "user" / "note.md").write_text("Path {{ docs.path }} literal.\n")
    assert load_user_skill(skills, "note") == "Path {{ docs.path }} literal.\n"


def test_render_error_raises_and_emits_no_skill_load_event(tmp_path: Path) -> None:
    import pytest

    from imp.agents.prompts import PromptRenderError

    ctx = _ctx(tmp_path)
    wf = ctx.quest_context.workflow_dir
    (wf / "skills" / "bad.md").write_text("{{ bad_ns.x }}\n")
    with pytest.raises(PromptRenderError):
        make_load_skill_tool().invoke(ctx, {"name": "bad"})
    events = list(JsonlEventLog(tmp_path / "events.jsonl").read())
    assert not any(e.get("type") == "skill_load" for e in events)


def test_agent_loaded_user_fallback_skill_stays_raw(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    wf = ctx.quest_context.workflow_dir
    (wf / "skills" / "user").mkdir(parents=True)
    (wf / "skills" / "user" / "proj.md").write_text("Literal {{ docs.path }} stays.\n")
    out = make_load_skill_tool().invoke(ctx, {"name": "proj"})
    assert out == "Literal {{ docs.path }} stays.\n"
    events = list(JsonlEventLog(tmp_path / "events.jsonl").read())
    assert any(e.get("type") == "skill_load" for e in events)


def test_all_bundled_skills_render_without_residual_placeholders() -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.agents.skills import load_internal_skill
    from imp.assets import bundled_skills_dir
    from imp.engine.state import InMemoryEventLog

    skills_dir = bundled_skills_dir()
    md_files = sorted(skills_dir.glob("*.md"))
    assert md_files, "no bundled skills found"
    for md in md_files:
        body = load_internal_skill(skills_dir, md.stem)
        delivered = render_prompt(
            body,
            build_context(quest_title="t", event_log=InMemoryEventLog()),
        )
        assert "{{" not in delivered, f"{md.name} leaked a template placeholder"
        assert "}}" not in delivered, f"{md.name} leaked a template placeholder"
