"""The threshold and finalize personas name only the imp-knowledge hub and
include the shared grounding, memory-authoring, and sandbox-limits partials."""

from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog, JsonlEventLog

PARTIALS = Path("src/imp/assets/partials")
FRONT_DOOR = Path("src/imp/assets/partials/front-door.md")


def _threshold() -> str:
    wf = load_workflow(bundled_workflow_dir("default"))
    return wf.agents["threshold"].prompt_template_source


def _finalize() -> str:
    wf = load_workflow(bundled_workflow_dir("default"))
    return wf.agents["finalize"].prompt_template_source


def test_threshold_names_only_hub() -> None:
    fd = FRONT_DOOR.read_text()
    assert "imp-knowledge" in fd
    assert '{% include "partials/imp-grounding.md" %}' in fd
    assert "imp-introspection" not in fd
    assert "imp-memory" not in fd


def test_threshold_hats_name_only_hub() -> None:
    fd = FRONT_DOOR.read_text()
    concierge = fd[fd.index("**concierge**"):fd.index("**scout**")]
    assert "imp-knowledge" in concierge
    assert "imp-introspection" not in concierge
    scout = fd[fd.index("**scout**"):fd.index("**brainstorming")]
    assert "imp-knowledge" in scout


def test_threshold_includes_grounding_partial() -> None:
    fd = FRONT_DOOR.read_text()
    assert '{% include "partials/imp-grounding.md" %}' in fd
    partial = (PARTIALS / "imp-grounding.md").read_text().lower()
    assert "do not answer from memory" in partial
    assert "how imp works" in partial


def test_finalize_includes_grounding_partial() -> None:
    assert '{% include "partials/imp-grounding.md" %}' in _finalize()


def test_threshold_action_rule_present() -> None:
    fd = FRONT_DOOR.read_text()
    assert "## Acting on imp" in fd
    assert "/save" in fd
    assert "/keep" not in fd
    assert "/abandon" in fd
    assert "/clear" in fd
    assert "confirm" in fd.lower()


def test_threshold_preserves_speak_plainly() -> None:
    fd = FRONT_DOOR.read_text()
    assert "## Speak plainly" in fd
    assert "never surface these internal terms" in fd


def test_threshold_introduces_no_new_cli_command_or_read_tool() -> None:
    fd = FRONT_DOOR.read_text()
    body = _threshold()
    for banned in ("list_quests", "show_change",
                   "imp explain", "imp doctor", "imp workflow show"):
        assert banned not in fd, banned
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


def test_threshold_orientation_cue_requires_reading_first() -> None:
    fd = FRONT_DOOR.read_text()
    assert "## Orienting before you offer" in fd
    low = fd.lower()
    assert "manifest" in low
    assert "explorer" in low
    assert "established stack" in low
    assert "greenfield" in low
    assert "open design question" in low
    assert "how does imp work" in low
    assert "read before you speak" in low
    marker = "## Orienting before you offer"
    start = fd.index(marker)
    section = fd[start:fd.index("{% include", start)].lower()
    assert "speak plainly" in section


def test_threshold_includes_sandbox_limits_partial() -> None:
    assert '{% include "partials/sandbox-limits.md" %}' in FRONT_DOOR.read_text()


def test_finalize_includes_sandbox_limits_partial() -> None:
    assert '{% include "partials/sandbox-limits.md" %}' in _finalize()


def test_threshold_and_finalize_render_sandbox_limits_partial(tmp_path) -> None:
    # A within-line sentinel from the partial: proves the include actually
    # resolves into each rendered prompt, not merely that the directive text is
    # present in the source.
    log = JsonlEventLog(tmp_path / "events.jsonl")
    wf = load_workflow(bundled_workflow_dir("default"))
    for name, extra_vars in (("threshold", {}), ("finalize", {"auto_merge": False})):
        body = wf.agents[name].prompt_template_source
        rendered = render_prompt(
            body,
            build_context(
                quest_title="t", event_log=log, quest_id="abc",
                quest_home=".imp/quests/abc", vars=extra_vars),
            workflow_dir=bundled_workflow_dir("default"))
        assert "only the user can type it" in rendered, name


def test_grounding_partial_points_at_the_corpus_path_variable() -> None:
    text = (PARTIALS / "imp-grounding.md").read_text()
    # the partial names the hub as an index into the corpus at the injected path
    assert "{{ docs.path }}" in text
    assert "corpus" in text.lower()
    # the stale spoke-era phrasing is gone (the deeper skills no longer exist)
    assert "deeper skill" not in text.lower()


def test_threshold_concierge_routes_to_corpus_not_removed_spokes() -> None:
    fd = FRONT_DOOR.read_text()
    concierge = fd[fd.index("**concierge**"):fd.index("**scout**")]
    assert "corpus" in concierge.lower()
    assert "the skills it maps to" not in concierge


def test_sandbox_limits_partial_documents_the_hatch_and_the_dump_recipe() -> None:
    text = (PARTIALS / "sandbox-limits.md").read_text()
    # Self-contained section header.
    assert "## When the sandbox blocks you" in text
    # The hatch is not debugger-only: a masked-path read is a first-class reason to recommend it.
    assert "or a read of a masked path" in text
    # The lifecycle: the sandbox re-arms itself at step end; `/sandbox on` only hurries that.
    assert "re-enables itself when the step ends" in text
    # The old copy told the user to re-enable it by hand; that clause is gone, not re-wrapped.
    assert "afterward to re-confine" not in text
    # (a) the /sandbox off escape hatch and its human-only property.
    assert "/sandbox off" in text
    assert "human-only" in text
    assert "only the user can type it" in text
    # (b) `/sandbox on` survives as the way to re-confine sooner than step end.
    assert "/sandbox on" in text
    # names a concrete live-process debugger so "when to recommend" is grounded.
    assert "gdb" in text
    # (c) the SIGUSR1 self-dump path: trigger, pid discovery, and dump file.
    assert "kill -USR1 <pid>" in text
    # (d) neither path is hard-coded: the home is interpolated, so a draft quest
    # (no worktree, hence no `.imp/quests/<id>` symlink) gets a resolvable path.
    assert ".imp/quests" not in text
    for home in (".imp/quests/abc", "/home/u/.imp/projects/p/quests/abc"):
        rendered = render_prompt(
            text,
            build_context(quest_title="t", event_log=InMemoryEventLog(),
                          quest_id="abc", quest_home=home),
            workflow_dir=bundled_workflow_dir("default"))
        assert f"{home}/lock" in rendered, home
        assert f"{home}/stack-dump.txt" in rendered, home


def test_threshold_surfaces_dependency_subsection_and_carveout_exception() -> None:
    fd = FRONT_DOOR.read_text()
    assert "## When the work depends on another quest" in fd
    assert "**dependency of this quest**, not a reason to spawn a separate one" in fd
    assert "imp edit {{ quest.id }} --depends-on <id>" in fd
    assert "Keep the work in this quest — don't split it into a child." in fd
    assert "except `imp edit {{ quest.id }} --depends-on`" in fd
    assert "--depends-on` / `--base`" in fd
    assert "to record a dependency before going live" not in fd


def test_threshold_documents_changing_the_base_branch() -> None:
    fd = FRONT_DOOR.read_text()
    assert "## Changing the base branch" in fd
    # The remedy for reading the wrong branch, and the two guardrails.
    assert ("`imp edit {{ quest.id }} --base <branch>` — the branch must already exist, "
            "and only") in fd
    assert "design turns on with `git show <branch>:<path>`." in fd
    assert "top of an unmerged quest use `--depends-on`, not a base change." in fd
    # Placement: immediately after the dependency section, before the backlog section.
    assert (fd.index("## When the work depends on another quest")
            < fd.index("## Changing the base branch")
            < fd.index("## Close redundant backlog quests at approval"))
