"""The front-door partial's undecided-settings block: verbatim, positioned with
the orientation guidance, and driven by project.undecided at both front doors
(default/threshold and minimal/front-door)."""

from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.env import ALL_DECIDED, Undecided
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog

FRONT_DOOR = Path("src/imp/assets/partials/front-door.md")

BLOCK = """{% if project.undecided %}
**Undecided project settings.** imp raises each of these once, when the repo can
answer it. Pin the value in this quest's spec for the implementer to set in
`imp/config.yaml` alongside the code — or pin `false` when the project genuinely
needs none, so it stops being raised. If this quest can't answer one, leave it.
{% if project.undecided.test_cmd_fast %}
- `test_cmd_fast` — the lint+typecheck+fast-test command run after each round of
  coding. Unset, that check is a no-op and every acceptance criterion you write
  goes unenforced (e.g. `ruff check . && pytest -q`).
{% endif %}
{% if project.undecided.worktree_setup %}
- `worktree_setup` — a command run in each new quest worktree before the first
  step. Unset, a fresh worktree holds only tracked files: no `.venv`,
  `node_modules` or `vendor`, which `git worktree` does not materialize
  (e.g. `npm ci`).
{% endif %}
{% if project.undecided.pythonpath %}
- `pythonpath` — worktree-relative Python import paths. Unset in an
  editable-installed Python project, imp's test gate imports the stale main
  checkout instead of the worktree under test (e.g. `[src]`).
{% endif %}
{% endif %}"""

FRAMING = "**Undecided project settings.**"
FAST = "`test_cmd_fast` — the lint+typecheck+fast-test command"
SETUP = "`worktree_setup` — a command run in each new quest worktree"
ROOTS = "`pythonpath` — worktree-relative Python import paths"


def _render(workflow: str, agent: str, undecided: Undecided) -> str:
    wf = load_workflow(bundled_workflow_dir(workflow))
    return render_prompt(
        wf.agents[agent].prompt_template_source,
        build_context(quest_title="t", event_log=InMemoryEventLog(),
                      undecided=undecided),
        workflow_dir=bundled_workflow_dir(workflow),
    )


def test_block_is_verbatim_and_sits_with_the_orientation_guidance() -> None:
    fd = FRONT_DOOR.read_text()
    assert BLOCK in fd
    orient = fd.index("Anything that touches this project does — read before you speak")
    principles = fd.index('{% include "partials/spec-design-principles.md" %}')
    assert orient < fd.index(BLOCK) < principles


def test_threshold_raises_only_the_undecided_settings() -> None:
    rendered = _render("default", "threshold", Undecided(worktree_setup=True))
    assert FRAMING in rendered
    assert SETUP in rendered
    assert FAST not in rendered
    assert ROOTS not in rendered


def test_all_decided_renders_no_block_at_all() -> None:
    for workflow, agent in (("default", "threshold"), ("minimal", "front-door")):
        rendered = _render(workflow, agent, ALL_DECIDED)
        assert FRAMING not in rendered
        assert FAST not in rendered
        assert SETUP not in rendered
        assert ROOTS not in rendered


def test_minimal_front_door_raises_an_undecided_setting() -> None:
    rendered = _render("minimal", "front-door", Undecided(test_cmd_fast=True))
    assert FRAMING in rendered
    assert FAST in rendered
    assert SETUP not in rendered
