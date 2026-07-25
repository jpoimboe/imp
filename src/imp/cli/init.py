"""`imp init`: scaffold imp/ in a project.

Spec §4: split output. A committed imp/project.yaml (workflow ref + empty
vars — NO bindings, NO theme) and a bootstrapped ~/.config/imp/config.yaml
(providers + theme + editing_mode). On a clone that already carries the
committed imp/project.yaml, init is idempotent: it never overwrites the
project file and only bootstraps the machine's user config if absent.

Committed config+workflows live under imp/ (tracked by git).
Runtime state (quests, locks, etc.) lives under .imp/ (gitignored wholesale).

Tier→model bindings come from the provider-code floor now, so init asks no
model question — it just records the enabled providers + detected theme. A
fresh interactive init then hands off to an agent-driven setup conversation
(default workflow, integration branch, fast test command, import root), so it
needs a working provider plus an OS sandbox (or an accepted unsandboxed offer)
and stops with an actionable message if either is missing. The non-interactive
path (`--use-defaults`, or an auto-selected no-TTY run) writes defaults with no
model call.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from imp.agents.interactive import (
    InteractiveFailure,
    InteractiveResult,
    run_interactive_agent_sync,
)
from imp.assets import (
    bundled_assets_dir,
    resolve_workflow_dir,
    selectable_workflow_names,
)
from imp.cli.prompt import Prompt
from imp.cli.pythonpath_detect import (
    format_pythonpath_yaml,
    resolve_detection_provider,
    validate_detected_roots,
)
from imp.cli.verify_detect import (
    detect_test_command,
    format_test_cmd_fast_yaml,
)
from imp.engine import worktree
from imp.engine.loader import parse_agent_file
from imp.engine.sandbox import SandboxConfig, sandbox_available
from imp.engine.types import OutputField
from imp.remote.drivers import REGISTRY

_DEFAULT_WORKFLOW = "default"

_INIT_COMMIT_PATHS: tuple[str, ...] = (
    "imp/project.yaml",
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
    """imp init needs an integration branch but couldn't determine one — the
    prompt couldn't read input (non-TTY / EOF), or no branch was resolvable."""


@dataclass(frozen=True, slots=True)
class InitSettings:
    """The settings the shared writer serializes into imp/project.yaml."""
    default_workflow: str
    default_base: str
    test_cmd_fast: str | None  # None omits the block; "" writes an explicit opt-out
    pythonpath: list[str]      # validated roots; empty writes the teaching hint


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


def _raise_eof(_prompt: str = "") -> str:
    raise EOFError


def _repo_is_unborn(project_root: Path) -> bool:
    """True when the repo has no commits yet — `git rev-parse --verify HEAD` fails."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=project_root, capture_output=True, text=True,
    ).returncode != 0


def _resolve_init_base(
    project_root: Path, *, git_created: bool,
    input_fn: Callable[[str], str],
) -> str:
    """The integration branch to record as `default-base` on a fresh init.

    1. origin/HEAD resolves -> use it, no prompt.
    2. Fresh setup (git init this run) or an unborn repo -> use the current /
       unborn branch, no prompt (no other candidate exists).
    3. Otherwise (existing repo with commits, no remote default) -> prompt,
       suggesting the current branch; Enter accepts it, a typed name overrides.

    Raises IntegrationBranchUnresolved when the prompt cannot read input, or
    when the user gives no branch and there is no current branch to fall back on.
    """
    remote = worktree.origin_head_branch(project_root)
    if remote is not None:
        return remote
    current = worktree.checked_out_branch(project_root)
    if (git_created or _repo_is_unborn(project_root)) and current is not None:
        return current
    suffix = f" [{current}]" if current else ""
    try:
        answer = input_fn(
            f"Which branch should quests integrate into?{suffix} "
        ).strip()
    except EOFError as e:
        raise IntegrationBranchUnresolved(
            "imp init needs an integration branch but can't read input "
            "(non-interactive terminal). Re-run `imp init` interactively, or add "
            "`default-base: <branch>` to imp/project.yaml."
        ) from e
    resolved = answer or current
    if not resolved:
        raise IntegrationBranchUnresolved(
            "imp init needs an integration branch but none was given. Re-run "
            "`imp init` and name the branch quests should integrate into."
        )
    return resolved


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

# Providers driven by a local CLI rather than an HTTP API — no env-var prompt
# at init time, and omitted from the informational env hints. codex
# authenticates via its own ~/.codex/auth.json (codex login), like claude_code.
_PROVIDERS_WITHOUT_ENV: frozenset[str] = frozenset({"claude_code", "codex"})


def _default_detect_theme() -> str:
    """Real terminal-theme detection (dark | light | ansi). Wrapped so a failure
    in the OSC-11 path can never abort init — any error degrades to 'ansi'."""
    try:
        from imp.cli.term_theme import detect_theme
        return detect_theme()
    except Exception:
        return "ansi"


def _ensure_imp_gitignore(project_root: Path) -> None:
    """Ensure `.imp/` (runtime control-root state) is gitignored; idempotent."""
    project_gi = project_root / ".gitignore"
    existing = project_gi.read_text() if project_gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    if ".imp/" not in existing_lines:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        project_gi.write_text(existing + sep + "\n# imp\n.imp/\n")


def _build_project_config(default_workflow: str, default_base: str) -> dict[str, Any]:
    """The committed imp/project.yaml body: chosen workflow + resolved integration
    branch + empty vars. No bindings (the provider-code floor supplies tiers);
    no theme (user-homed)."""
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
    """The bootstrapped ~/.config/imp/config.yaml providers block (theme/editing_mode
    appended separately as commented raw lines)."""
    return {
        "providers": list(providers),
    }


_CREDENTIAL_HINTS: dict[str, str] = {
    "claude_code": "install the Claude CLI and run `claude login`",
    "anthropic": "set ANTHROPIC_API_KEY in your environment",
    "openai": "set OPENAI_API_KEY in your environment",
    "codex": "install the Codex CLI and run `codex login`",
}


def _read_user_config(user_path: Path) -> dict[str, Any] | None:
    if not user_path.exists():
        return None
    try:
        return yaml.safe_load(user_path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _bootstrap_user_config(
    user_path: Path, providers: list[str], theme: str, *, allow_unsandboxed: bool,
) -> None:
    """Write the machine user config once; no-op if it exists, never clobbering a hand-edit."""
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


_TEST_CMD_FAST_COMMENT = (
    "\n# Fast test gate: lint + typecheck + fast tests, run after each\n"
    "# implement loop to catch regressions. Empty string opts out.\n"
    "# The slow gate (test_cmd_slow) runs the full suite once per quest\n"
    "# and falls back to test_cmd_fast when unset. Timeouts default to\n"
    "# 120s (fast) / 300s (slow) — override with test_cmd_fast_timeout /\n"
    "# test_cmd_slow_timeout.\n"
)

_PYTHONPATH_BLOCK_COMMENT = (
    "\n# Worktree-relative import roots prepended to gate/agent\n"
    "# subprocess PYTHONPATH, so an editable-installed package resolves\n"
    "# to the worktree under test, not the stale main checkout.\n"
)


def _facts_hint() -> str:
    return (
        "\n# Project facts: human-authored values any agent prompt can read via\n"
        "# {{ project.<key> }}. All commented out -- uncomment and edit to expose\n"
        "# facts to your agents. For standing prose instructions to agents (what\n"
        "# the old guidance key did), write markdown under imp/memory/ instead --\n"
        "# e.g. imp/memory/<workflow>/<step>.md; see imp/memory/all.md for the layout.\n"
        "# facts:\n"
        "#   test_command: make test\n"
        "#   stack: python 3.12, pytest\n"
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
        ("bold", "Which providers do you want to enable?\n"),
    ]
    for i, name in enumerate(available):
        arrow = "> " if i == cursor else "  "
        box = "[x] " if i in selected else "[ ] "
        style = "ansicyan bold" if i == cursor else ""
        out.append((style, f"{arrow}{box}{name}\n"))
    out.append((
        "ansibrightblack",
        "↑↓ move · space toggle · enter confirm · esc cancel",
    ))
    return out


def _interactive_provider_picker(available: list[str]) -> list[str]:
    """Inline cursor picker rendered with prompt_toolkit.

    Renders in place (not a full-screen dialog):
        Which providers do you want to enable?
        > [ ] claude_code
          [ ] anthropic
          [ ] openai
        up/down move | space toggle | enter confirm | esc cancel

    Keys: ↑↓ move cursor, space toggles, enter confirms (≥1 required),
    ctrl-c / esc raise KeyboardInterrupt.

    Style names match `imp log`'s palette via prompt_toolkit's ANSI aliases:
    bold for the header, ansicyan for the active row, ansibrightblack
    (= our `gray`/`dim`) for the help line.
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
    body = Window(
        content=control,
        height=Dimension(min=len(available) + 2, max=len(available) + 2),
    )
    app: Any = Application(
        layout=Layout(HSplit([body])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    return list(app.run())


def write_init_files(project_root: Path, settings: InitSettings) -> bool:
    """Write imp/project.yaml + the memory scaffold from `settings`, ensure `.imp/`
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
    if settings.pythonpath:
        pythonpath_block = (
            _PYTHONPATH_BLOCK_COMMENT + format_pythonpath_yaml(settings.pythonpath))
    else:
        pythonpath_block = _pythonpath_hint()
    (imp_dir / "project.yaml").write_text(
        body + test_block + pythonpath_block + _facts_hint())
    _write_memory_scaffold(imp_dir, settings.default_workflow)
    _ensure_imp_gitignore(project_root)
    return _commit_init_files(
        project_root,
        extra_paths=(f"imp/memory/{settings.default_workflow}/all.md",),
    )


def _workflow_choices(project_root: Path) -> list[tuple[str, str]]:
    """(name, description) for every SELECTABLE workflow — the agent's context for
    framing the default-workflow choice. The description is the FIRST line of each
    workflow.yaml `description:` block (the pipeline summary): the bundled blocks
    are multi-paragraph, so taking one line keeps the kickoff compact and free of
    mid-line newlines. Empty when absent."""
    out: list[tuple[str, str]] = []
    for name in selectable_workflow_names(project_root):
        wf_yaml = resolve_workflow_dir(project_root, name) / "workflow.yaml"
        desc = ""
        if wf_yaml.is_file():
            data = yaml.safe_load(wf_yaml.read_text()) or {}
            raw = str(data.get("description", "")).strip()
            desc = raw.splitlines()[0].strip() if raw else ""
        out.append((name, desc))
    return out


def _build_init_kickoff(
    *,
    workflows: list[tuple[str, str]],
    base_hint: str | None,
    git_exists: bool,
) -> str:
    """The setup-context kickoff message handed to the setup agent."""
    lines = ["Repository setup context for this conversation:", ""]
    lines.append("Available workflows (name — description):")
    for name, desc in workflows:
        lines.append(f"  {name} — {desc}" if desc else f"  {name}")
    lines.append("")
    if git_exists:
        lines.append("This project is already a git repository.")
    else:
        lines.append("This directory is NOT yet a git repository; imp needs one.")
    if base_hint:
        lines.append(
            f"The repository's default integration branch appears to be `{base_hint}`.")
    else:
        lines.append("No default integration branch could be detected from git.")
    lines.append("")
    lines.append("Begin the setup conversation now.")
    return "\n".join(lines)


def _conversation_failure_message(
    provider_name: str, failure: InteractiveFailure,
) -> str:
    if failure.category in ("auth", "config"):
        hint = _CREDENTIAL_HINTS.get(provider_name, "check its credential")
        return (f"imp reached {provider_name} but setup couldn't authenticate. "
                f"{hint[0].upper()}{hint[1:]}, then re-run `imp init`.")
    return (f"Setup didn't finish ({failure.detail}). "
            "Re-run `imp init` when you're ready.")


def _run_init_conversation(
    *,
    project_root: Path,
    provider_name: str,
    model_name: str,
    allow_unsandboxed: bool,
    color: bool,
    output_fn: Callable[[str], None],
) -> InteractiveResult:
    """Default conversation seam: run the read-only setup agent — bundled outside
    every workflow, so a project-local imp/workflows/default cannot shadow it — in
    the foreground host and return its InteractiveResult. Tests monkeypatch this."""
    assets = bundled_assets_dir()
    agent_def = parse_agent_file(assets / "init.md")
    # Pinned at runtime: the per-project valid set (bundled plus committed
    # imp/workflows/*) can't be declared statically in the agent's frontmatter.
    schema = agent_def.output_schema
    fields = tuple(
        OutputField(name="default_workflow", type="enum",
                    values=tuple(selectable_workflow_names(project_root)),
                    required=True)
        if f.name == "default_workflow" else f
        for f in schema.fields
    )
    agent_def = replace(agent_def, output_schema=replace(schema, fields=fields))
    kickoff = _build_init_kickoff(
        workflows=_workflow_choices(project_root),
        base_hint=worktree.origin_head_branch(project_root),
        git_exists=_in_git_repo(project_root))
    reader = Prompt(color=color).ask_async
    return run_interactive_agent_sync(
        agent_def=agent_def, provider_name=provider_name, model_name=model_name,
        initial_message=kickoff, project_root=project_root,
        asset_dir=assets,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=allow_unsandboxed),
        read_line=reader, output_fn=output_fn, color=color, title="imp setup")


def run_init(
    project_root: Path,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    picker_fn: Callable[[list[str]], list[str]] | None = None,
    detect_theme_fn: Callable[[], str] | None = None,
) -> None:
    from imp.providers.effective import user_config_path

    imp_dir = project_root / "imp"
    project_file = imp_dir / "project.yaml"
    already_initialized = project_file.is_file()

    output_fn(f"Setting up imp in {project_root}")

    user_path = user_config_path()
    wrote_user_config = not user_path.exists()

    # --- providers + theme: skip the picker when the user config already
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
        picked_this_run = True
    output_fn(f"Enabled: {', '.join(providers)}")
    for prov in providers:
        if prov in _PROVIDERS_WITHOUT_ENV:
            continue
        output_fn(f"  {prov} → {prov.upper()}_API_KEY (set this env var)")
    if picked_this_run and theme != "ansi":
        output_fn(f"Detected a {theme} terminal — colors tuned to match.")

    # --- re-init idempotency: an already-committed project file means no
    #     conversation; just bootstrap a missing machine config and re-commit ---
    if already_initialized:
        if wrote_user_config:
            _bootstrap_user_config(
                user_path, providers, theme, allow_unsandboxed=False)
        _ensure_git_repo_silent(project_root)
        _ensure_imp_gitignore(project_root)
        committed = _commit_init_files(project_root)
        output_fn("")
        output_fn("Project already initialized (imp/project.yaml is committed).")
        if wrote_user_config:
            output_fn("Bootstrapped your machine config (providers, theme).")
        else:
            output_fn("Your machine config is already set up — nothing to do.")
        output_fn(f"  {user_path}  (your machine: providers, theme)")
        if committed:
            output_fn("Committed imp/project.yaml.")
        return

    # --- fresh init: credential + sandbox gate (a working provider is required
    #     for the setup conversation) ---
    resolved = resolve_detection_provider(providers)
    if resolved is None:
        if wrote_user_config:
            _bootstrap_user_config(
                user_path, providers, theme, allow_unsandboxed=False)
        width = max(len(p) for p in providers)
        output_fn(
            "imp needs a working model provider to finish setting up this project.")
        output_fn("None of the providers you enabled has a usable credential yet:")
        for prov in providers:
            hint = _CREDENTIAL_HINTS.get(prov, "set up its credential")
            output_fn(f"  {prov:<{width}} → {hint}")
        output_fn("Set one up, then re-run `imp init`.")
        return
    provider_name, model_name = resolved

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

    # Persist the machine config before the conversation so a re-run never
    # re-asks the questions already answered.
    if wrote_user_config:
        _bootstrap_user_config(
            user_path, providers, theme, allow_unsandboxed=allow_unsandboxed)

    # --- hand off to the setup conversation ---
    output_fn("Now let's finish setting up this project.")
    result = _run_init_conversation(
        project_root=project_root,
        provider_name=provider_name, model_name=model_name,
        allow_unsandboxed=allow_unsandboxed,
        color=theme in ("dark", "light"), output_fn=output_fn)

    if isinstance(result, InteractiveFailure):
        output_fn(_conversation_failure_message(provider_name, result))
        return
    output = result.output
    if output.get("status") != "Success":
        output_fn(
            "Setup didn't finish — the assistant reported it couldn't complete. "
            "Re-run `imp init` when you're ready.")
        return

    # --- host applies the agent's decisions and writes the files ---
    if output.get("git_init"):
        _ensure_git_repo_silent(project_root)
    elif not _in_git_repo(project_root):
        # The agent declined git in a repo-less dir: a clean message beats an
        # uncaught CalledProcessError from _commit_init_files' `git status`.
        output_fn(
            "imp needs a git repository but setup left this directory without "
            "one. Re-run `imp init` and allow git setup.")
        return
    roots = list((output.get("pythonpath") or {}).get("roots", []))
    settings = InitSettings(
        default_workflow=str(output["default_workflow"]),
        default_base=str(output["default_base"]),
        test_cmd_fast=str(output["test_cmd_fast"]),
        pythonpath=validate_detected_roots(project_root, roots))
    committed = write_init_files(project_root, settings)

    output_fn("")
    output_fn("Created:")
    output_fn("  imp/project.yaml")
    output_fn(f"  {user_path}  (your machine: providers, theme)")
    if committed:
        output_fn("Committed imp/project.yaml.")
    output_fn("")
    output_fn('Try: imp new "Add a dark-mode toggle"')


def run_init_noninteractive(
    project_root: Path,
    *,
    output_fn: Callable[[str], None],
    workflow: str | None = None,
    base: str | None = None,
    pythonpath: list[str] | None = None,
    detect_verify_fn: Callable[[Path], str | None] | None = None,
) -> None:
    """Provider-free, non-interactive `imp init`: write imp/project.yaml from
    defaults (+ overrides) with no model call. Both `--use-defaults` and the
    no-TTY auto-headless fallback land here. Never prompts — resolves the
    integration branch from git or raises IntegrationBranchUnresolved."""
    from imp.providers.effective import user_config_path

    workflow = workflow or _DEFAULT_WORKFLOW
    imp_dir = project_root / "imp"
    already_initialized = (imp_dir / "project.yaml").is_file()
    user_path = user_config_path()
    wrote_user_config = not user_path.exists()

    if already_initialized:
        _ensure_git_repo_silent(project_root)  # a copied project.yaml may sit in a non-git dir
        if wrote_user_config:
            _bootstrap_user_config(user_path, [], "ansi", allow_unsandboxed=False)
        _ensure_imp_gitignore(project_root)
        _commit_init_files(project_root)
        output_fn("Project already initialized (imp/project.yaml is committed).")
        return

    git_created = _ensure_git_repo_silent(project_root)
    if base is not None:
        default_base = base
    else:
        default_base = _resolve_init_base(
            project_root, git_created=git_created, input_fn=_raise_eof)
    output_fn(f"Integration branch: {default_base}")

    detect_cmd = detect_verify_fn or detect_test_command
    test_cmd_fast = detect_cmd(project_root)  # None omits; a heuristic hit fills it
    roots = validate_detected_roots(project_root, pythonpath) if pythonpath else []

    settings = InitSettings(
        default_workflow=workflow, default_base=default_base,
        test_cmd_fast=test_cmd_fast, pythonpath=roots)
    committed = write_init_files(project_root, settings)

    if wrote_user_config:
        _bootstrap_user_config(user_path, [], "ansi", allow_unsandboxed=False)

    output_fn("Created:")
    output_fn("  imp/project.yaml")
    if committed:
        output_fn("Committed imp/project.yaml.")
