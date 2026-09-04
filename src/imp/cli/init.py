# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Tier→model bindings come from the provider-code floor now, so init asks no
model question — it just records the enabled providers + detected theme. A
fresh interactive init then detects this project's settings (default workflow,
integration branch, fast test command, worktree setup command, import root) and
confirms them on one screen. Init needs no model provider on any path — only an
OS sandbox (or an accepted unsandboxed offer), and it stops with an actionable
message without one. The non-interactive path (`--use-defaults`, or an
auto-selected no-TTY run) writes defaults with no model call.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple

import yaml
from ruamel.yaml.error import YAMLError

from imp.assets import (
    selectable_workflow_names,
)
from imp.cli import palette as pal
from imp.cli import splash
from imp.cli.pythonpath_detect import (
    ImportRoots,
    detect_import_roots,
    format_pythonpath_yaml,
    validate_detected_roots,
)
from imp.cli.setup_detect import (
    DetectedSetupCommand,
    detect_worktree_setup,
    format_worktree_setup_yaml,
)
from imp.cli.verify_detect import (
    DetectedTestCommand,
    detect_test_command,
    format_test_cmd_fast_yaml,
)
from imp.engine import worktree
from imp.engine._yaml import config_yaml_error, load_config_yaml
from imp.engine.sandbox import sandbox_available
from imp.remote.drivers import REGISTRY

_DEFAULT_WORKFLOW = "default"

_INIT_COMMIT_PATHS: tuple[str, ...] = (
    "imp/config.yaml",
    ".gitignore",
    "imp/memory/all.md",
)

# Both scaffolds are wholly wrapped in one HTML comment, so strip_html_comments
# reduces them to empty and they inject nothing until the user writes prose.
_MEMORY_GLOBAL_SCAFFOLD = """<!--
imp memory: standing instructions you author as plain markdown, which imp layers
onto its agents. Files resolve broad -> narrow for a running step:

  imp/memory/all.md                # every agent, every workflow  (THIS FILE)
  imp/memory/<workflow>/all.md     # every step of that workflow
  imp/memory/<workflow>/<step>.md  # just that step of that workflow

A filename is a step name; `all.md` means "everything at this level". HTML
comments like this one are stripped before injection, so this scaffold injects
nothing until you write real prose below it.

WARNING: this file is injected into EVERY agent, so it bloats EVERY context.
Use it EXTREMELY sparingly -- prefer a per-step file
(imp/memory/<workflow>/<step>.md) scoped to just the agent that needs it.
-->
"""

def _memory_workflow_scaffold(workflow: str) -> str:
    return f"""<!--
imp memory for every step of the `{workflow}` workflow. Write standing instructions
below to reach all steps of this workflow, or scope to a single step with a
sibling file named for that step (e.g. imp/memory/{workflow}/threshold.md).
`all.md` means "everything at this level". HTML comments are stripped before
injection, so this scaffold injects nothing until you add real prose below.

This file fans out to EVERY step of the workflow, so use it sparingly --
prefer a per-step file scoped to just the step that needs it.
-->
"""


class IntegrationBranchUnresolved(Exception):
    """imp init needs an integration branch but git offers none (detached HEAD)."""


class GitIdentityUnavailable(Exception):
    """git can build no author identity here, so imp init cannot commit."""


_GIT_NO_IDENTITY = (
    "git has no author identity here, so imp can't commit. "
    "Set one, then re-run `imp init`:\n"
    "\n"
    "  git config user.name 'Your Name'\n"
    "  git config user.email 'you@example.com'"
)


@dataclass(frozen=True, slots=True)
class InitSettings:
    """The settings the shared writer serializes into imp/config.yaml. The three
    stack-dependent fields are three-state: None never decided, False decided as
    "this project needs none", anything else the value."""
    default_workflow: str
    default_base: str
    test_cmd_fast: str | Literal[False] | None
    worktree_setup: str | Literal[False] | None
    pythonpath: list[str] | Literal[False] | None


def _in_git_repo(project_root: Path) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_root, capture_output=True, text=True,
    ).returncode == 0


def _ensure_git_repo_silent(project_root: Path) -> bool:
    """Non-interactive `git init` when no repo exists; returns True iff one was
    created this run. No prompt — the non-interactive path never asks."""
    if _in_git_repo(project_root):
        return False
    subprocess.run(["git", "init"], cwd=project_root, check=True,
                   capture_output=True, text=True)
    return True


def _git_can_commit(project_root: Path) -> bool:
    """True when git can resolve an author identity. `git var` walks the same
    chain `git commit` does — GIT_AUTHOR_* env, then config, then $EMAIL/GECOS —
    so a config-only check would call an ordinary host unconfigured."""
    return subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"],
        cwd=project_root, capture_output=True, text=True,
    ).returncode == 0


def _repo_is_unborn(project_root: Path) -> bool:
    """True when the repo has no commits yet — `git rev-parse --verify HEAD` fails."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=project_root, capture_output=True, text=True,
    ).returncode != 0


class BaseDetection(NamedTuple):
    """The integration branch git offers, with the label the confirmation screen
    shows for where it came from. Absent branch means no BaseDetection at all,
    so a provenance can never stand without a branch."""
    branch: str
    provenance: str


def _resolve_init_base(
    project_root: Path, *, git_created: bool,
) -> BaseDetection | None:
    """The integration branch to record as `default-base` on a fresh init, with
    where it came from. Never prompts — the confirmation screen owns correction.

    1. origin/HEAD resolves -> that branch.
    2. Fresh setup (git init this run) or an unborn repo -> the current branch.
    3. Otherwise -> the current branch, adopted as-is.

    None when git offers no branch at all (detached HEAD).
    """
    remote = worktree.origin_head_branch(project_root)
    if remote is not None:
        return BaseDetection(remote, "from origin/HEAD")
    current = worktree.checked_out_branch(project_root)
    if current is None:
        return None
    if git_created or _repo_is_unborn(project_root):
        return BaseDetection(current, "new repo")
    return BaseDetection(current, "current branch")


def _commit_init_files(project_root: Path, *, extra_paths: tuple[str, ...] = ()) -> bool:
    """Stage and commit init files by explicit pathspec; return True iff a commit
    was created. Only paths that exist on disk are staged — the imp/memory/
    scaffolds are absent on an already-initialized re-init, and `git add` with a
    pathspec matching no file exits non-zero."""
    paths = [p for p in list(_INIT_COMMIT_PATHS) + list(extra_paths) if (project_root / p).exists()]
    if not paths:
        return False
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *paths],
        cwd=project_root, capture_output=True, text=True, check=True)
    if not status.stdout.strip():
        return False
    subprocess.run(["git", "add", "--", *paths],
                   cwd=project_root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "imp: initialize project",
         "--", *paths],
        cwd=project_root, check=True, capture_output=True, text=True)
    return True


# The order providers are presented in the picker. Bindings (tier→model) are
# owned by the provider-code floor now, so init no longer carries a model table.
_PROVIDER_DISPLAY_ORDER: tuple[str, ...] = (
    "claude_code", "anthropic", "openai", "codex",
)


def _default_detect_theme() -> str:
    """Real terminal-theme detection (dark | light | ansi). Wrapped so a failure
    in the OSC-11 path can never abort init — any error degrades to 'ansi'."""
    try:
        from imp.cli.term_theme import detect_theme
        return detect_theme()
    except Exception:
        return "ansi"


def _ensure_imp_gitignore(project_root: Path) -> None:
    """Ensure `.imp/` (the per-worktree quest-home symlink dir) is gitignored; idempotent."""
    project_gi = project_root / ".gitignore"
    existing = project_gi.read_text() if project_gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    if ".imp/" not in existing_lines:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        project_gi.write_text(existing + sep + "\n# imp\n.imp/\n")


def _build_project_config(default_workflow: str, default_base: str) -> dict[str, Any]:
    """The committed imp/config.yaml body: chosen workflow + resolved integration
    branch + empty vars. No bindings (the provider-code floor supplies tiers);
    no theme (global-homed)."""
    return {
        "default-workflow": default_workflow,
        "default-base": default_base,
        "vars": {},
    }


def _write_memory_scaffold(imp_dir: Path, workflow: str) -> None:
    """Create the two inert comment-only memory files a fresh init ships:
    the global imp/memory/all.md and imp/memory/<workflow>/all.md matching the
    chosen workflow. Both are wholly commented, so they inject nothing until the
    user edits them."""
    memory_dir = imp_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "all.md").write_text(_MEMORY_GLOBAL_SCAFFOLD)
    wf_dir = memory_dir / workflow
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "all.md").write_text(_memory_workflow_scaffold(workflow))


def _build_user_config(providers: list[str]) -> dict[str, Any]:
    """The bootstrapped ~/.imp/config.yaml providers block (theme/editing_mode
    appended separately as commented raw lines)."""
    return {
        "providers": list(providers),
    }


def _read_user_config(user_path: Path) -> dict[str, Any] | None:
    """The machine config's settings, or None when the file is absent. Raises
    when it exists but is not a readable mapping: init repairs `providers:` in
    place now, and repairing text it cannot read would be guesswork."""
    from imp.cli.config_write import ConfigWriteError

    if not user_path.exists():
        return None
    try:
        data = load_config_yaml(user_path.read_text())
    except YAMLError as err:
        raise ConfigWriteError(
            f"invalid YAML in {user_path.name}: {config_yaml_error(err)}") from err
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigWriteError(
            f"{user_path.name} must be a mapping of settings, "
            f"got {type(data).__name__}")
    return data


def _bootstrap_user_config(
    user_path: Path, providers: list[str], theme: str, *, allow_unsandboxed: bool,
) -> None:
    """Write the machine's global config once; no-op if it exists, never clobbering a hand-edit."""
    if user_path.exists():
        return
    user_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox_block = (
        "sandbox:\n  allow_unsandboxed: true\n" if allow_unsandboxed else "")
    user_path.write_text(
        yaml.safe_dump(_build_user_config(providers),
                       sort_keys=False, default_flow_style=False)
        + f"theme: {theme}  # dark | light | ansi\n"
        + "editing_mode: emacs  # emacs | vi\n"
        + sandbox_block
        + _remote_hint())


def _set_user_providers(user_path: Path, providers: list[str]) -> None:
    """Write `providers:` into an existing machine config through the same
    comment-preserving seam as `imp config set providers`. The caller has
    already parsed the file (_read_user_config), so unreadable text cannot
    reach apply_set here."""
    from imp.cli.config_cmd import layer_label
    from imp.cli.config_edit import apply_set
    from imp.cli.config_write import (
        ConfigWriteError,
        land,
        scratch_dir,
        scratch_seam,
        verify,
    )

    result = apply_set(user_path.read_text(), ("providers",), providers)
    with scratch_seam(scratch_dir(user_path), user_path.name) as scratch:
        if not verify(scratch, result.text, ("providers",), providers):
            raise ConfigWriteError(
                f"refusing to write {layer_label(user_path)} — "
                "'providers' would not read back as given")
        land(scratch, user_path)


_TEST_CMD_FAST_COMMENT = (
    "\n# Fast test gate: lint + typecheck + fast tests, run after each\n"
    "# implement loop to catch regressions. false declares the project needs none.\n"
    "# The slow gate (test_cmd_slow) runs the full suite once per quest\n"
    "# and falls back to test_cmd_fast when unset. Timeouts default to\n"
    "# 120s (fast) / 300s (slow) — override with test_cmd_fast_timeout /\n"
    "# test_cmd_slow_timeout.\n"
)

_WORKTREE_SETUP_COMMENT = (
    "\n# Command run in each new quest worktree before the first step, for toolchains\n"
    "# that keep dependencies in a gitignored per-tree directory (.venv,\n"
    "# node_modules, vendor) that `git worktree` does not materialize. false\n"
    "# declares the project needs none.\n"
)

_PYTHONPATH_BLOCK_COMMENT = (
    "\n# Worktree-relative import roots prepended to gate/agent\n"
    "# subprocess PYTHONPATH, so an editable-installed package resolves\n"
    "# to the worktree under test, not the stale main checkout.\n"
)


def _memory_pointer() -> str:
    return (
        "\n# Standing prose instructions for agents are markdown under imp/memory/\n"
        "# -- e.g. imp/memory/<workflow>/<step>.md; see imp/memory/all.md for the\n"
        "# layout. Nothing is injected until you edit those files.\n"
    )


def _pythonpath_hint() -> str:
    return (
        "\n# pythonpath: worktree-relative import roots prepended to the gate/agent\n"
        "# subprocess PYTHONPATH. Set this when this project is installed editable\n"
        "# (pip install -e) and imported by package name — without it imp's test gate\n"
        "# runs the STALE main-checkout code instead of the worktree under test.\n"
        "# Use [src] for a src-layout package, [.] for a flat layout.\n"
        "# pythonpath: [src]\n"
    )


def _remote_hint() -> str:
    lines = [
        "",
        "# Remote driver: watch and drive a run from a chat surface. Enable ONE",
        "# built-in by name (credentials stay in the environment, never here):",
        "# remote:",
        "#   driver: telegram",
        "# Built-ins and the env vars each needs:",
    ]
    for name in sorted(REGISTRY):
        entry = REGISTRY[name]
        lines.append(f"#   {name} — {entry.description} env: {', '.join(entry.required_env)}")
    return "\n".join(lines) + "\n"


# claude_code (Anthropic) is the recommended single-provider default (review
# architecture spec, M2). Pre-select it in the picker so the common path produces
# a single-provider config; multi-select stays available for codex/openai.
_RECOMMENDED_DEFAULT = "claude_code"


def _initial_selection(available: list[str]) -> set[int]:
    """Indices pre-selected when the picker opens: the recommended default if it
    is offered, else nothing (the picker still requires >=1 before confirm)."""
    if _RECOMMENDED_DEFAULT in available:
        return {available.index(_RECOMMENDED_DEFAULT)}
    return set()


def _picker_rows(
    available: list[str], cursor: int, selected: set[int],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [
        ("bold", "Which providers do you want to enable?\n\n"),
    ]
    for i, name in enumerate(available):
        arrow = "> " if i == cursor else "  "
        box = "[x] " if i in selected else "[ ] "
        style = "ansiyellow bold" if i == cursor else ""
        out.append((style, f"{arrow}{box}{name}\n"))
    out.append((
        "ansibrightblack",
        "\n↑↓ move · space toggle · enter confirm · esc cancel",
    ))
    return out


def _interactive_provider_picker(available: list[str]) -> list[str]:
    """Inline cursor picker rendered with prompt_toolkit.

    Renders in place (not a full-screen dialog):
        Which providers do you want to enable?

        > [ ] claude_code
          [ ] anthropic
          [ ] openai

        ↑↓ move · space toggle · enter confirm · esc cancel

    Keys: ↑↓ move cursor, space toggles, enter confirms (≥1 required),
    ctrl-c / esc raise KeyboardInterrupt.

    Style names match `imp log`'s palette via prompt_toolkit's ANSI aliases:
    bold for the header, ansiyellow for the active row — the hue `pal.caret()`
    emits under the `ansi` theme, which is still the active one this early in
    init — and ansibrightblack (= our `gray`/`dim`) for the help line.
    """
    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension

    cursor = 0
    selected: set[int] = _initial_selection(available)  # pre-select the recommended default

    def _render() -> list[tuple[str, str]]:
        return _picker_rows(available, cursor, selected)

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event: Any) -> None:
        nonlocal cursor
        cursor = (cursor - 1) % len(available)

    @kb.add("down")
    def _down(_event: Any) -> None:
        nonlocal cursor
        cursor = (cursor + 1) % len(available)

    @kb.add(" ")
    def _space(_event: Any) -> None:
        if cursor in selected:
            selected.remove(cursor)
        else:
            selected.add(cursor)

    @kb.add("enter")
    def _enter(event: Any) -> None:
        if not selected:
            return  # require at least one
        event.app.exit(result=[available[i] for i in sorted(selected)])

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _cancel(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    control = FormattedTextControl(text=_render, focusable=True, show_cursor=False)
    # Derived from the rows themselves so the layout can't drift from the render.
    height = sum(text.count("\n") for _style, text in _render()) + 1
    body = Window(content=control, height=Dimension(min=height, max=height))
    app: Any = Application(
        layout=Layout(HSplit([body])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    return list(app.run())


_CONFIRM_HEADER = "Here's how imp will set up this project:"
_CONFIRM_HINT = "↑↓ move · space change · enter accept · esc cancel"
_NO_VALUE = "(none)"
_NONE_NEEDED = "(none needed)"
_YOU_SET_THIS = "you set this"
_NO_TESTS_DETECTED = "nothing detected — imp runs no tests until one is set"
_NO_SETUP_DETECTED = "nothing detected — no command runs in new worktrees"
_NO_SUCH_ROOTS = "no such directory in this repo — nothing recorded"
_NO_BRANCH_DETECTED = "nothing detected"
_CUSTOM_WORKFLOW = "custom workflow"

# Hand-written, never derived: every workflow.yaml `description:` opens with its
# internal step-name pipeline, which must not reach a user's first screen.
_WORKFLOW_BLURBS = {
    "default": "full pipeline — spec, plan, implement, review, merge",
    "minimal": "scope, build on a branch, merge it yourself",
}


class ScreenRow(IntEnum):
    WORKFLOW = 0
    BRANCH = 1
    TESTS = 2
    WORKTREE_SETUP = 3
    IMPORT_ROOT = 4


_ROW_LABELS: dict[ScreenRow, str] = {
    ScreenRow.WORKFLOW: "workflow",
    ScreenRow.BRANCH: "branch",
    ScreenRow.TESTS: "tests",
    ScreenRow.WORKTREE_SETUP: "worktree setup",
    ScreenRow.IMPORT_ROOT: "import root",
}


@dataclass(frozen=True, slots=True)
class InitScreenState:
    """What the confirmation screen shows and, on accept, writes. `test_cmd_fast`,
    `worktree_setup` and `pythonpath` carry InitSettings' semantics exactly, so
    accepting needs no reinterpretation. The workflow row's provenance is derived,
    never stored."""
    workflow: str
    branch: str
    branch_provenance: str
    test_cmd_fast: str | Literal[False] | None
    tests_provenance: str
    worktree_setup: str | Literal[False] | None
    setup_provenance: str
    pythonpath: list[str] | Literal[False] | None
    roots_provenance: str


def _screen_state_from_detection(
    *,
    workflow: str,
    base: BaseDetection | None,
    tests: DetectedTestCommand | None,
    setup: DetectedSetupCommand | None,
    roots: ImportRoots,
) -> InitScreenState:
    return InitScreenState(
        workflow=workflow,
        branch=base.branch if base else "",
        branch_provenance=base.provenance if base else _NO_BRANCH_DETECTED,
        test_cmd_fast=tests.command if tests else None,
        tests_provenance=tests.provenance if tests else _NO_TESTS_DETECTED,
        worktree_setup=setup.command if setup else None,
        setup_provenance=setup.provenance if setup else _NO_SETUP_DETECTED,
        pythonpath=list(roots.roots) or None,
        roots_provenance=roots.provenance,
    )


def _workflow_blurb(name: str) -> str:
    return _WORKFLOW_BLURBS.get(name, _CUSTOM_WORKFLOW)


def _cycle_workflow(state: InitScreenState, workflows: list[str]) -> InitScreenState:
    # The caller passes the list the state was seeded from, so the name is in it.
    return replace(
        state,
        workflow=workflows[(workflows.index(state.workflow) + 1) % len(workflows)])


def _display_value(value: str | list[str] | Literal[False] | None) -> str:
    if value is None:
        return _NO_VALUE
    if value is False:
        return _NONE_NEEDED
    return ", ".join(value) if isinstance(value, list) else value


def _row_cells(state: InitScreenState) -> dict[ScreenRow, tuple[str, str, str]]:
    """Each row's (label, value, provenance): the screen pads these into columns."""
    values: dict[ScreenRow, tuple[str, str]] = {
        ScreenRow.WORKFLOW: (state.workflow, _workflow_blurb(state.workflow)),
        ScreenRow.BRANCH: (state.branch or _NO_VALUE, state.branch_provenance),
        ScreenRow.TESTS: (_display_value(state.test_cmd_fast), state.tests_provenance),
        ScreenRow.WORKTREE_SETUP: (
            _display_value(state.worktree_setup), state.setup_provenance),
        ScreenRow.IMPORT_ROOT: (
            _display_value(state.pythonpath), state.roots_provenance),
    }
    return {row: (_ROW_LABELS[row], value, note)
            for row, (value, note) in values.items()}


def _settings_summary(settings: InitSettings) -> list[str]:
    """The screen erases itself on accept, so scrollback would otherwise keep no
    record of the settings the user confirmed."""
    label_w = max(len(label) for label in _ROW_LABELS.values())
    values: dict[ScreenRow, str] = {
        ScreenRow.WORKFLOW: settings.default_workflow,
        ScreenRow.BRANCH: settings.default_base or _NO_VALUE,
        ScreenRow.TESTS: _display_value(settings.test_cmd_fast),
        ScreenRow.WORKTREE_SETUP: _display_value(settings.worktree_setup),
        ScreenRow.IMPORT_ROOT: _display_value(settings.pythonpath),
    }
    return [f"  {_ROW_LABELS[row]:<{label_w}}  {values[row]}" for row in ScreenRow]


def _confirm_rows(
    state: InitScreenState, cursor: ScreenRow,
) -> list[tuple[str, str]]:
    """The confirmation screen's fragments — the sibling of `_picker_rows`.
    Three columns, each padded to its widest entry."""
    cells = _row_cells(state)
    label_w = max(len(label) for label, _value, _note in cells.values())
    value_w = max(len(value) for _label, value, _note in cells.values())
    out: list[tuple[str, str]] = [("", "\n"), ("bold", f"{_CONFIRM_HEADER}\n\n")]
    for row in ScreenRow:
        label, value, note = cells[row]
        arrow = "> " if row is cursor else "  "
        style = "ansiyellow bold" if row is cursor else ""
        out.append((style, f"{arrow}{label:<{label_w}}  {value:<{value_w}}  {note}\n"))
    out.append(("ansibrightblack", f"\n{_CONFIRM_HINT}"))
    return out


# The workflow row is absent by construction: `space` cycles it in place.
_EDIT_PROMPTS: dict[ScreenRow, str] = {
    ScreenRow.BRANCH: "Integration branch: ",
    ScreenRow.TESTS: "Fast test command: ",
    ScreenRow.WORKTREE_SETUP: "Worktree setup command: ",
    ScreenRow.IMPORT_ROOT: "Import roots (comma-separated): ",
}


def _edit_default(state: InitScreenState, row: ScreenRow) -> str:
    if row is ScreenRow.BRANCH:
        return state.branch
    if row is ScreenRow.TESTS:
        return state.test_cmd_fast or ""
    if row is ScreenRow.WORKTREE_SETUP:
        return state.worktree_setup or ""
    return ", ".join(state.pythonpath) if state.pythonpath else ""


def _apply_row_edit(
    project_root: Path, state: InitScreenState, row: ScreenRow, answer: str,
) -> InitScreenState:
    """The row's value replaced by what the user typed, its provenance rewritten."""
    if row is ScreenRow.BRANCH:
        return replace(
            state, branch=answer.strip(), branch_provenance=_YOU_SET_THIS)
    if row is ScreenRow.TESTS:
        return replace(
            state, test_cmd_fast=answer.strip() or False,
            tests_provenance=_YOU_SET_THIS)
    if row is ScreenRow.WORKTREE_SETUP:
        return replace(
            state, worktree_setup=answer.strip() or False,
            setup_provenance=_YOU_SET_THIS)
    entered = [part.strip() for part in answer.split(",") if part.strip()]
    if not entered:
        return replace(state, pythonpath=False, roots_provenance=_YOU_SET_THIS)
    validated = validate_detected_roots(project_root, entered)
    if not validated:
        return replace(state, pythonpath=None, roots_provenance=_NO_SUCH_ROOTS)
    return replace(state, pythonpath=validated, roots_provenance=_YOU_SET_THIS)


class ScreenAccepted(NamedTuple):
    """`enter` on the confirmation screen: write this state."""
    state: InitScreenState


class _ScreenAction(Enum):
    ACCEPT = "accept"
    EDIT = "edit"


def _default_row_prompt(message: str, default: str) -> str:
    from prompt_toolkit import prompt as pt_prompt
    try:
        return str(pt_prompt(message, default=default))
    except EOFError:  # ctrl-d cancels setup, the same exit esc offers on the screen
        raise KeyboardInterrupt from None


def _run_confirm_app(
    state: InitScreenState, cursor: ScreenRow, workflows: list[str],
) -> tuple[_ScreenAction, InitScreenState, ScreenRow]:
    """One pass of the screen. Returns why it exited, plus the state and cursor
    it exited with. Cancel (esc / ctrl-c) raises KeyboardInterrupt, as the
    provider picker does. `erase_when_done` keeps a re-render from stacking."""
    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension

    row = cursor
    current = state

    def _render() -> list[tuple[str, str]]:
        return _confirm_rows(current, row)

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event: Any) -> None:
        nonlocal row
        row = ScreenRow((row - 1) % len(ScreenRow))

    @kb.add("down")
    def _down(_event: Any) -> None:
        nonlocal row
        row = ScreenRow((row + 1) % len(ScreenRow))

    @kb.add(" ")
    def _space(event: Any) -> None:
        nonlocal current
        if row is ScreenRow.WORKFLOW:
            current = _cycle_workflow(current, workflows)
        else:
            event.app.exit(result=_ScreenAction.EDIT)

    @kb.add("enter")
    def _enter(event: Any) -> None:
        if not current.branch:
            return  # an unwritable config is unreachable
        event.app.exit(result=_ScreenAction.ACCEPT)

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _cancel(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    control = FormattedTextControl(text=_render, focusable=True, show_cursor=False)
    height = sum(text.count("\n") for _style, text in _render()) + 1
    body = Window(content=control, height=Dimension(min=height, max=height))
    app: Any = Application(
        layout=Layout(HSplit([body])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
    )
    action: _ScreenAction = app.run()
    return action, current, row


def _interactive_confirm_screen(
    project_root: Path,
    state: InitScreenState,
    *,
    workflows: list[str],
    prompt_fn: Callable[[str, str], str] = _default_row_prompt,
) -> ScreenAccepted:
    """The detected-and-confirmed setup screen. Editing a row leaves the screen,
    asks for the value with the current one pre-filled, and re-renders."""
    cursor = ScreenRow.WORKFLOW
    while True:
        action, state, cursor = _run_confirm_app(state, cursor, workflows)
        if action is _ScreenAction.ACCEPT:
            return ScreenAccepted(state)
        state = _apply_row_edit(
            project_root, state, cursor,
            prompt_fn(_EDIT_PROMPTS[cursor], _edit_default(state, cursor)))


def write_init_files(project_root: Path, settings: InitSettings) -> bool:
    """Write imp/config.yaml + the memory scaffold from `settings`, ensure `.imp/`
    is gitignored, and commit the init files. Returns True iff a commit was created.
    Callers ensure the git repo exists first (base resolution needs it)."""
    imp_dir = project_root / "imp"
    imp_dir.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        _build_project_config(settings.default_workflow, settings.default_base),
        sort_keys=False, default_flow_style=False)
    if settings.test_cmd_fast is None:
        test_block = ""
    else:
        test_block = (
            _TEST_CMD_FAST_COMMENT + format_test_cmd_fast_yaml(settings.test_cmd_fast))
    if settings.worktree_setup is None:
        setup_block = ""
    else:
        setup_block = (
            _WORKTREE_SETUP_COMMENT
            + format_worktree_setup_yaml(settings.worktree_setup))
    if settings.pythonpath is None:
        pythonpath_block = _pythonpath_hint()
    else:
        pythonpath_block = (
            _PYTHONPATH_BLOCK_COMMENT + format_pythonpath_yaml(settings.pythonpath))
    (imp_dir / "config.yaml").write_text(
        body + test_block + setup_block + pythonpath_block + _memory_pointer())
    _write_memory_scaffold(imp_dir, settings.default_workflow)
    _ensure_imp_gitignore(project_root)
    return _commit_init_files(
        project_root,
        extra_paths=(f"imp/memory/{settings.default_workflow}/all.md",),
    )


_GIT_QUESTION = "imp needs a git repository. Initialize one here? [Y/n] "
_GIT_DECLINED = ("imp needs a git repository to track quests. "
                 "Re-run `imp init` when you're ready.")

_GIT_IDENTITY_LEAD_IN = (
    "git has no author identity here, so imp can't commit.\n"
    "imp will set one for this repo only — it goes in .git/config, not your machine."
)


def _ensure_git_identity_interactive(
    project_root: Path, *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    """Ask for a name and email and write them repo-locally when git can build no
    author identity. Both are asked before either is written — the probe is
    all-or-nothing — and an empty answer to either writes nothing and raises."""
    if _git_can_commit(project_root):
        return
    output_fn(_GIT_IDENTITY_LEAD_IN)
    name = input_fn("  Your name: ").strip()
    email = input_fn("  Your email: ").strip()
    if not name or not email:
        raise GitIdentityUnavailable(_GIT_NO_IDENTITY)
    for key, value in (("user.name", name), ("user.email", email)):
        subprocess.run(["git", "config", key, value],
                       cwd=project_root, check=True, capture_output=True, text=True)
    output_fn(f"Set {name} <{email}> in .git/config.")


def _confirm_init_settings(
    project_root: Path, state: InitScreenState,
) -> InitSettings:
    """The confirmation screen. Cancelling it raises KeyboardInterrupt."""
    outcome = _interactive_confirm_screen(
        project_root, state, workflows=selectable_workflow_names(project_root))
    state = outcome.state
    return InitSettings(
        default_workflow=state.workflow,
        default_base=state.branch,
        test_cmd_fast=state.test_cmd_fast,
        worktree_setup=state.worktree_setup,
        pythonpath=state.pythonpath)


def run_init(
    project_root: Path,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    picker_fn: Callable[[list[str]], list[str]] | None = None,
    detect_theme_fn: Callable[[], str] | None = None,
) -> None:
    from imp.providers import auth_hint_lines
    from imp.providers.paths import global_config_path

    color_on = pal.should_use_color(sys.stdout)
    imp_dir = project_root / "imp"
    project_file = imp_dir / "config.yaml"
    already_initialized = project_file.is_file()

    output_fn("Setting up imp in "
              + pal.ansi(str(project_root), "wordmark", enabled=color_on))

    user_path = global_config_path()
    wrote_user_config = not user_path.exists()

    # --- providers + theme: skip the picker when the global config already
    #     declares providers (a re-run must not re-ask) ---
    existing_cfg = _read_user_config(user_path)
    if existing_cfg and existing_cfg.get("providers"):
        providers = [str(p) for p in existing_cfg["providers"]]
        theme = str(existing_cfg.get("theme", "ansi"))
        picked_this_run = False
    else:
        pick = picker_fn or _interactive_provider_picker
        providers = pick(list(_PROVIDER_DISPLAY_ORDER))
        if not providers:
            raise ValueError("at least one provider must be selected")
        theme = (detect_theme_fn or _default_detect_theme)()
        # Everything below renders in the detected theme; IMP_THEME still wins.
        if pal.theme_override() is None:
            pal.set_theme(theme)
        picked_this_run = True
    output_fn(pal.ansi(f"Enabled: {', '.join(providers)}", "dim", enabled=color_on))
    for line in auth_hint_lines(providers):
        output_fn(pal.ansi(line, "dim", enabled=color_on))
    if picked_this_run and theme != "ansi":
        output_fn(pal.ansi(
            f"Detected a {theme} terminal — colors tuned to match.",
            "dim", enabled=color_on))

    # --- re-init idempotency: an already-committed project file means no
    #     confirmation screen; just bootstrap a missing machine config and re-commit ---
    if already_initialized:
        if wrote_user_config:
            _bootstrap_user_config(
                user_path, providers, theme, allow_unsandboxed=False)
        _ensure_git_repo_silent(project_root)
        _ensure_git_identity_interactive(
            project_root, input_fn=input_fn, output_fn=output_fn)
        _ensure_imp_gitignore(project_root)
        committed = _commit_init_files(project_root)
        output_fn("")
        output_fn("Project already initialized (imp/config.yaml is committed).")
        if wrote_user_config:
            output_fn("Bootstrapped your machine config (providers, theme).")
        else:
            output_fn("Your machine config is already set up — nothing to do.")
        output_fn(f"  {user_path}  (your machine: providers, theme)")
        if committed:
            output_fn("Committed imp/config.yaml.")
        return

    # --- fresh init: the sandbox gate (init itself needs no model provider) ---
    allow_unsandboxed = False
    if not sandbox_available():
        answer = input_fn(
            "bubblewrap (bwrap) isn't installed, so imp can't OS-sandbox the "
            "commands its agents run.\n"
            "Run imp without that sandbox for now? Agent shell commands won't be "
            "isolated from your machine. [y/N] "
        ).strip().lower()
        if answer in ("y", "yes"):
            allow_unsandboxed = True
            output_fn(
                "Running without an OS sandbox. Install bubblewrap later and "
                "remove sandbox.allow_unsandboxed to re-enable it.")
        else:
            if wrote_user_config:
                _bootstrap_user_config(
                    user_path, providers, theme, allow_unsandboxed=False)
            output_fn(
                "Install bubblewrap for a real filesystem boundary, then "
                "re-run `imp init`:")
            output_fn("  Fedora:  sudo dnf install bubblewrap")
            output_fn("  Debian:  sudo apt install bubblewrap")
            output_fn(
                f"Or set `sandbox.allow_unsandboxed: true` in {user_path} to "
                "proceed without one.")
            return

    # Persist the machine config before the confirmation screen so a re-run
    # never re-asks the questions already answered.
    if wrote_user_config:
        _bootstrap_user_config(
            user_path, providers, theme, allow_unsandboxed=allow_unsandboxed)

    output_fn("")
    if color_on:
        output_fn(splash.splash_block(
            theme=pal.active_theme(),
            width=shutil.get_terminal_size((80, 24)).columns, color=True))

    # --- git, detection, and the confirmation screen ---
    if not _in_git_repo(project_root):
        answer = input_fn(_GIT_QUESTION).strip().lower()
        if answer not in ("", "y", "yes"):
            output_fn(_GIT_DECLINED)
            return
    git_created = _ensure_git_repo_silent(project_root)
    _ensure_git_identity_interactive(
        project_root, input_fn=input_fn, output_fn=output_fn)
    settings = _confirm_init_settings(
        project_root,
        _screen_state_from_detection(
            workflow=_DEFAULT_WORKFLOW,
            base=_resolve_init_base(project_root, git_created=git_created),
            tests=detect_test_command(project_root),
            setup=detect_worktree_setup(project_root),
            roots=detect_import_roots(project_root)))
    committed = write_init_files(project_root, settings)

    output_fn("")
    output_fn(pal.ansi("Configured:", "wordmark", enabled=color_on))
    for line in _settings_summary(settings):
        output_fn(pal.ansi(line, "dim", enabled=color_on))
    output_fn("")
    output_fn(pal.ansi("Created:", "wordmark", enabled=color_on))
    output_fn(pal.ansi("  imp/config.yaml", "dim", enabled=color_on))
    output_fn(pal.ansi(
        f"  {user_path}  (your machine: providers, theme)", "dim", enabled=color_on))
    if committed:
        output_fn(pal.ansi("Committed imp/config.yaml.", "dim", enabled=color_on))
    output_fn("")
    output_fn("Try: " + pal.ansi(
        'imp new "Add a dark-mode toggle"', "wordmark", enabled=color_on))


def run_init_noninteractive(
    project_root: Path,
    *,
    output_fn: Callable[[str], None],
    workflow: str | None = None,
    base: str | None = None,
    pythonpath: list[str] | Literal[False] | None = None,
    detect_verify_fn: Callable[[Path], DetectedTestCommand | None] | None = None,
) -> None:
    """Provider-free, non-interactive `imp init`: write imp/config.yaml from
    defaults (+ overrides) with no model call. Both `--use-defaults` and the
    no-TTY auto-headless fallback land here. Never prompts — resolves the
    integration branch from git, or raises IntegrationBranchUnresolved when git
    offers none, and raises GitIdentityUnavailable when git can build no author
    identity to commit with."""
    from imp.providers.paths import global_config_path

    workflow = workflow or _DEFAULT_WORKFLOW
    imp_dir = project_root / "imp"
    already_initialized = (imp_dir / "config.yaml").is_file()
    user_path = global_config_path()
    wrote_user_config = not user_path.exists()

    if already_initialized:
        _ensure_git_repo_silent(project_root)  # a copied config.yaml may sit in a non-git dir
        if not _git_can_commit(project_root):
            raise GitIdentityUnavailable(_GIT_NO_IDENTITY)
        if wrote_user_config:
            _bootstrap_user_config(user_path, [], "ansi", allow_unsandboxed=False)
        _ensure_imp_gitignore(project_root)
        _commit_init_files(project_root)
        output_fn("Project already initialized (imp/config.yaml is committed).")
        return

    git_created = _ensure_git_repo_silent(project_root)
    if not _git_can_commit(project_root):
        raise GitIdentityUnavailable(_GIT_NO_IDENTITY)
    if base is not None:
        default_base = base
    else:
        detected_base = _resolve_init_base(project_root, git_created=git_created)
        if detected_base is None:
            raise IntegrationBranchUnresolved(
                "imp init couldn't determine an integration branch (HEAD is "
                "detached). Re-run with `--base <branch>`, or add "
                "`default-base: <branch>` to imp/config.yaml.")
        default_base = detected_base.branch
    output_fn(f"Integration branch: {default_base}")

    detect_cmd = detect_verify_fn or detect_test_command
    detected = detect_cmd(project_root)
    test_cmd_fast = detected.command if detected else None  # None omits the block
    detected_setup = detect_worktree_setup(project_root)
    roots: list[str] | Literal[False] | None
    if pythonpath is None:
        roots = detect_import_roots(project_root).roots or None
    elif pythonpath is False:
        roots = False
    else:
        validated = validate_detected_roots(project_root, pythonpath)
        if not validated:
            output_fn(
                f"--pythonpath: no such directory in this repo: "
                f"{', '.join(pythonpath)} — leaving import roots unset.")
            roots = None
        else:
            roots = validated

    settings = InitSettings(
        default_workflow=workflow, default_base=default_base,
        test_cmd_fast=test_cmd_fast,
        worktree_setup=detected_setup.command if detected_setup else None,
        pythonpath=roots)
    committed = write_init_files(project_root, settings)

    if wrote_user_config:
        _bootstrap_user_config(user_path, [], "ansi", allow_unsandboxed=False)

    output_fn("Created:")
    output_fn("  imp/config.yaml")
    if committed:
        output_fn("Committed imp/config.yaml.")
