"""The sandbox-limits partial states imp's write bounds ahead of the escape hatches,
and the old partial name survives nowhere under src/ or tests/."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_partials_dir, bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog

PARTIAL = Path("src/imp/assets/partials/sandbox-limits.md")
# Split so the stale-name scan below never matches its own source file.
_OLD_NAME = "sandbox-" + "debugging"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_partial_states_the_write_bounds_ahead_of_the_hatches() -> None:
    flat = _flat(PARTIAL.read_text())
    assert flat.startswith("## What you cannot write")
    assert (
        "binds all of `.git/refs/heads`, `.git/refs/stash`, and the main "
        "`.git/index` read-only" in flat)
    assert "re-exposing only `refs/heads/imp/`" in flat
    # The doctrine sentence: base refs are the engine's and the user's alone.
    assert "Base branches move only by imp's engine or by the user — never by you." in flat
    assert "give the user the exact command to run in their own shell" in flat
    # git's misleading error is called out, so an agent reads a denial correctly.
    assert "blames a write error rather than a permission" in flat
    # The escape-hatch section appears below the write-bounds section.
    assert flat.index("## What you cannot write") < flat.index("## When the sandbox blocks you")


def test_partial_documents_the_pid_namespace_and_routes_usr1_through_sandbox_off() -> None:
    flat = _flat(PARTIAL.read_text())
    # The terminal clause is load-bearing: a program driven on a pty joins the
    # same namespace, so the sentence can no longer say "shell commands" alone.
    assert ("imp's sandbox runs all your shell commands — and any program you "
            "drive on a terminal — in one user and pid namespace of their own") in flat
    assert ("cannot attach a debugger (`gdb`, `py-spy`, `strace`) to a live process "
            "outside it") in flat
    assert "cannot see or signal any process it did not start" in flat
    assert "One escape hatch exists, plus one recipe that needs it." in flat
    # The USR1 recipe no longer works from inside the sandbox: it leads with /sandbox off.
    assert "**Stack-dumping a wedged imp process needs `/sandbox off`.**" in flat
    assert "means signalling a host pid, which the pid namespace puts out of reach" in flat
    assert "After `/sandbox off`, `kill -USR1 <pid>`" in flat
    # The stale claims are gone, not re-worded.
    assert "signals cross user namespaces" not in flat
    assert "needs no unsandboxing" not in flat
    assert "in a fresh user and pid namespace" not in flat
    assert "any process outside its own command" not in flat


def test_partial_tells_the_agent_the_toggle_resumes_its_step() -> None:
    flat = _flat(PARTIAL.read_text())
    resumes = ("Ask and end your turn — typing it resumes your step "
               "automatically with a note that the sandbox is off.")
    # It lands right after the human-only clause — where the agent has just been
    # told to ask — not stranded at the end of the section.
    assert "no agent or tool path reaches it. " + resumes in flat


def test_no_source_or_test_file_references_the_old_partial_name() -> None:
    assert not (PARTIAL.parent / f"{_OLD_NAME}.md").exists()
    hits = [
        str(p)
        for root in (Path("src"), Path("tests"))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".md", ".py", ".yaml", ".yml", ".sh"}
        and _OLD_NAME in p.read_text(errors="ignore")
    ]
    assert hits == []


# Every interactive agent that runs git writes, across both workflows.
_SITES = (
    ("default", "threshold", {}),
    ("default", "finalize", {"auto_merge": False}),
    ("minimal", "front-door", {}),
    ("minimal", "finalize", {}),
)


@pytest.mark.parametrize(("workflow", "agent", "extra_vars"), _SITES)
def test_interactive_git_writing_agents_render_the_write_bounds(
        workflow, agent, extra_vars) -> None:
    wf_dir = bundled_workflow_dir(workflow)
    body = load_workflow(wf_dir).agents[agent].prompt_template_source
    out = render_prompt(
        body,
        build_context(quest_title="t", event_log=InMemoryEventLog(), quest_id="abc",
                      quest_home=".imp/quests/abc", vars=extra_vars),
        workflow_dir=wf_dir)
    # A sentence only the write-bounds section carries: the include resolved here.
    assert "Base branches move only by imp's engine" in out
    assert "{% include" not in out
    # Fragments only the reframed hatch section carries — the include pulls it in too.
    assert "or a read of a masked path" in out
    assert "re-enables itself when the step ends" in out
    # The rewritten masking steer, and the grounding partial it defers to, both land.
    assert "so a filesystem search silently answers the wrong question" in out
    assert 'load_skill(name="imp-knowledge")' in out


_NEW_HEADING = "## imp state on disk is masked — load `imp-knowledge`"


def test_partial_steers_to_the_skill_and_names_no_cli_surface() -> None:
    text = PARTIAL.read_text()
    section = text.split(_NEW_HEADING + "\n")[1]
    lines = [ln for ln in section.splitlines() if ln.strip()]
    # The section is a markdown blockquote in the file and stays one.
    assert lines and all(ln.startswith("> ") for ln in lines)
    # Strip `> ` first: `_flat` alone leaves each `>` as a token, breaking wrap-spanning matches.
    flat = _flat(" ".join(ln.removeprefix("> ") for ln in lines))
    assert ("The sandbox masks this project's imp state — sibling quest homes and the "
            "archive are not on disk at all, so a filesystem search silently answers "
            "the wrong question.") in flat
    assert ("Never read or search other quests' or archived state off the filesystem, "
            "and never infer another quest's liveness from it.") in flat
    assert ("Load `imp-knowledge` for the CLI-based reads that answer the question "
            "correctly, resolved by the unsandboxed daemon.") in flat
    # One place documents the CLI — the skill — so this prompt cannot drift out of date.
    for banned in ("imp show", "imp list", "imp log", "imp search", "imp events",
                   "IMP_SESSION_SOCKET", "IMP_STATE_DIR", "events.jsonl", "`cat`"):
        assert banned not in text, banned


_WORKFLOWS = Path("src/imp/assets/workflows")
_INCLUDE = re.compile(r'\{%\s*include\s+"partials/([^"]+)\.md"\s*%\}')


def _include_closure(workflow_dir: Path, source: str) -> set[str]:
    seen: set[str] = set()
    pending = _INCLUDE.findall(source)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        local = workflow_dir / "partials" / f"{name}.md"
        path = local if local.is_file() else bundled_partials_dir() / f"{name}.md"
        pending.extend(_INCLUDE.findall(path.read_text()))
    return seen


def test_every_agent_carrying_sandbox_limits_also_carries_grounding() -> None:
    carriers: set[str] = set()
    for agents_dir in sorted(_WORKFLOWS.glob("*/agents")):
        wf_dir = agents_dir.parent
        for src in sorted(agents_dir.glob("*.md")):
            closure = _include_closure(wf_dir, src.read_text())
            if "sandbox-limits" not in closure:
                continue
            site = f"{wf_dir.name}/{src.stem}"
            carriers.add(site)
            # The steer defers to the skill, so grounding must reach the same prompt.
            assert "imp-grounding" in closure, site
    assert {"default/threshold", "default/finalize",
            "minimal/front-door", "minimal/finalize"} <= carriers
