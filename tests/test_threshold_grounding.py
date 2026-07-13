"""The threshold and finalize personas name only the imp-knowledge hub and
include the shared grounding, memory-authoring, and sandbox-debugging partials."""

from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog

PARTIALS = Path("src/imp/assets/workflows/imp/partials")


def _threshold() -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents["threshold"].prompt_template_source


def _finalize() -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents["finalize"].prompt_template_source


def test_threshold_names_only_hub() -> None:
    body = _threshold()
    assert "imp-knowledge" in body
    assert '{% include "partials/imp-grounding.md" %}' in body
    assert "imp-introspection" not in body
    assert "imp-memory" not in body


def test_threshold_hats_name_only_hub() -> None:
    body = _threshold()
    concierge = body[body.index("**concierge**"):body.index("**scout**")]
    assert "imp-knowledge" in concierge
    assert "imp-introspection" not in concierge
    scout = body[body.index("**scout**"):body.index("**brainstorming")]
    assert "imp-knowledge" in scout


def test_threshold_includes_grounding_partial() -> None:
    body = _threshold()
    assert '{% include "partials/imp-grounding.md" %}' in body
    partial = (PARTIALS / "imp-grounding.md").read_text().lower()
    assert "do not answer from memory" in partial
    assert "how imp works" in partial


def test_finalize_includes_grounding_partial() -> None:
    assert '{% include "partials/imp-grounding.md" %}' in _finalize()


def test_threshold_action_rule_present() -> None:
    body = _threshold()
    assert "## Acting on imp" in body
    # current-session mutations handed back via the slash commands
    assert "/save" in body
    assert "/abandon" in body
    assert "/clear" in body
    low = body.lower()
    # mutations proposed and confirmed first; other-change mutations on confirmation
    assert "confirm" in low


def test_threshold_preserves_speak_plainly() -> None:
    body = _threshold()
    assert "## Speak plainly" in body
    assert "never surface these internal terms" in body


def test_threshold_introduces_no_new_cli_command_or_read_tool() -> None:
    body = _threshold()
    for banned in ("list_quests", "show_change",
                   "imp explain", "imp doctor", "imp workflow show"):
        assert banned not in body, banned


def test_grounding_partial_carries_memory_rule_and_names_only_hub() -> None:
    text = (PARTIALS / "imp-grounding.md").read_text()
    low = text.lower()
    assert "## Grounding" in text
    assert "do not answer from memory" in low
    assert 'load_skill(name="imp-knowledge")' in text
    # hub-only rule: the partial names no spoke directly
    assert "imp-introspection" not in text
    assert "imp-memory" not in text


def test_threshold_has_lazy_orientation_cue() -> None:
    body = _threshold()
    # The cue lives in its own section.
    assert "## Orienting before you offer" in body
    low = body.lower()
    # Orient by reading the tree (manifest/build files) or dispatching an explorer.
    assert "manifest" in low
    assert "explorer" in low
    # Ground the offer in an established stack when one is found.
    assert "established stack" in low
    # Treat an empty/greenfield repo's stack as an open design question.
    assert "greenfield" in low
    assert "open design question" in low
    # Orientation is lazy / on demand — a pure imp-mechanics question does not
    # trigger a project-stack read.
    assert "how does imp work" in low
    assert "lazy" in low or "on demand" in low
    # The cue honors Speak plainly and surfaces no internal terms. Scope this to
    # the new section so the assertion can't pass on the body's pre-existing
    # "## Speak plainly" heading.
    marker = "## Orienting before you offer"
    start = body.index(marker)
    section = body[start:body.index("{% include", start)].lower()
    assert "speak plainly" in section


def test_threshold_includes_sandbox_debugging_partial() -> None:
    assert '{% include "partials/sandbox-debugging.md" %}' in _threshold()


def test_finalize_includes_sandbox_debugging_partial() -> None:
    assert '{% include "partials/sandbox-debugging.md" %}' in _finalize()


def test_threshold_and_finalize_render_sandbox_debugging_partial(tmp_path) -> None:
    # A within-line sentinel from the partial: proves the include actually
    # resolves into each rendered prompt, not merely that the directive text is
    # present in the source.
    log = JsonlEventLog(tmp_path / "events.jsonl")
    wf = load_workflow(bundled_workflow_dir("imp"))
    for name, extra_vars in (("threshold", {}), ("finalize", {"auto_merge": False})):
        body = wf.agents[name].prompt_template_source
        rendered = render_prompt(
            body,
            build_context(
                quest_title="t", event_log=log, quest_id="abc",
                quest_home=".imp/quests/abc", vars=extra_vars),
            workflow_dir=bundled_workflow_dir("imp"))
        assert "only the user can type it" in rendered, name


def test_sandbox_debugging_partial_documents_both_hatches() -> None:
    text = (PARTIALS / "sandbox-debugging.md").read_text()
    # Self-contained section header.
    assert "## Debugging live processes" in text
    # (a) the /sandbox off escape hatch and its human-only property.
    assert "/sandbox off" in text
    assert "human-only" in text
    assert "only the user can type it" in text
    # (b) the reminder to re-enable the sandbox afterward.
    assert "/sandbox on" in text
    # names a concrete live-process debugger so "when to recommend" is grounded.
    assert "gdb" in text
    # (c) the SIGUSR1 self-dump path: trigger, pid discovery, and dump file.
    assert "kill -USR1 <pid>" in text
    assert ".imp/quests/<id>/lock" in text
    assert ".imp/quests/<id>/stack-dump.txt" in text
