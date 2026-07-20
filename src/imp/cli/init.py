"""`imp init`: scaffold imp/ in a project.

Spec §4: split output. A committed imp/project.yaml (workflow ref + empty
vars — NO bindings, NO theme) and a bootstrapped ~/.config/imp/config.yaml
(providers + theme + editing_mode). On a clone that already carries the
committed imp/project.yaml, init is idempotent: it never overwrites the
project file and only bootstraps the machine's user config if absent.

Committed config+workflows live under imp/ (tracked by git).
Runtime state (quests, locks, etc.) lives under .imp/ (gitignored wholesale).

Tier→model bindings come from the provider-code floor now, so init asks no
model question — it just records the enabled providers + detected theme.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from imp.assets import resolve_workflow_dir
from imp.cli.pythonpath_detect import (
    DetectedRoots,
    DetectionFailed,
    detect_pythonpath_via_agent,
    format_pythonpath_yaml,
    is_python_packaging_project,
    resolve_detection_provider,
)
from imp.cli.verify_detect import (
    detect_test_command,
    format_test_cmd_fast_yaml,
    resolve_test_cmd_choice,
)
from imp.engine import worktree
from imp.engine.sandbox import SandboxConfig, sandbox_available
from imp.remote.drivers import REGISTRY

_DEFAULT_WORKFLOW = "imp"

_INIT_COMMIT_PATHS: tuple[str, ...] = (
    "imp/project.yaml",
    ".gitignore",
    "imp/memory/all.md",
    f"imp/memory/{_DEFAULT_WORKFLOW}/all.md",
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

_MEMORY_WORKFLOW_SCAFFOLD = """<!--
imp memory for every step of the `imp` workflow. Write standing instructions
below to reach all steps of this workflow, or scope to a single step with a
sibling file named for that step (e.g. imp/memory/imp/threshold.md).
`all.md` means "everything at this level". HTML comments are stripped before
injection, so this scaffold injects nothing until you add real prose below.

This file fans out to EVERY step of the workflow, so use it sparingly --
prefer a per-step file scoped to just the step that needs it.
-->
"""


class GitSetupDeclined(Exception):
    """The user declined `imp init`'s offer to set up a git repository."""


class IntegrationBranchUnresolved(Exception):
    """imp init needs an integration branch but couldn't determine one — the
    prompt couldn't read input (non-TTY / EOF), or no branch was resolvable."""


def _in_git_repo(project_root: Path) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_root, capture_output=True, text=True,
    ).returncode == 0


def _ensure_git_repo(
    project_root: Path, *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> bool:
    """Ensure `project_root` is a git repo. Returns True iff this call ran
    `git init` (a fresh repo was created this run), False if one already
    existed. Raises GitSetupDeclined when the user declines the offer."""
    if _in_git_repo(project_root):
        return False
    answer = input_fn("Initialize a git repository here? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        raise GitSetupDeclined(
            "imp needs a git repository — re-run `imp init` and allow git setup."
        )
    subprocess.run(["git", "init"], cwd=project_root, check=True,
                   capture_output=True, text=True)
    output_fn("Initialized a git repository.")
    return True


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


def _commit_init_files(project_root: Path) -> bool:
    """Stage and commit init files by explicit pathspec; return True iff a commit
    was created. Only paths that exist on disk are staged — the imp/memory/
    scaffolds are absent on an already-initialized re-init, and `git add` with a
    pathspec matching no file exits non-zero."""
    paths = [p for p in _INIT_COMMIT_PATHS if (project_root / p).exists()]
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


def _build_project_config(default_base: str) -> dict[str, Any]:
    """The committed imp/project.yaml: workflow ref + the resolved integration
    branch + empty vars. No bindings (the provider-code floor supplies tiers);
    no theme (user-homed). `default-base` is always written now — `imp init`
    resolves it up front (detect / ask / error), so `imp new` consumes it
    directly with no runtime fallback."""
    return {
        "default-workflow": _DEFAULT_WORKFLOW,
        "default-base": default_base,
        "vars": {},
    }


def _write_memory_scaffold(imp_dir: Path) -> None:
    """Create the two inert comment-only memory files a fresh init ships:
    the global imp/memory/all.md and the default-workflow imp/memory/<wf>/all.md.
    Both are wholly commented, so they inject nothing until the user edits them."""
    memory_dir = imp_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "all.md").write_text(_MEMORY_GLOBAL_SCAFFOLD)
    wf_dir = memory_dir / _DEFAULT_WORKFLOW
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "all.md").write_text(_MEMORY_WORKFLOW_SCAFFOLD)


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


def run_init(
    project_root: Path,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    picker_fn: Callable[[list[str]], list[str]] | None = None,
    detect_theme_fn: Callable[[], str] | None = None,
    detect_verify_fn: Callable[[Path], str | None] | None = None,
) -> None:
    from imp.providers.effective import user_config_path

    imp_dir = project_root / "imp"
    project_file = imp_dir / "project.yaml"
    already_initialized = project_file.is_file()

    git_created = _ensure_git_repo(
        project_root, input_fn=input_fn, output_fn=output_fn)

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
            # The interactive picker enforces ≥1; a custom picker_fn might not.
            raise ValueError("at least one provider must be selected")
        theme = (detect_theme_fn or _default_detect_theme)()
        picked_this_run = True
    output_fn(f"Enabled: {', '.join(providers)}")
    for prov in providers:
        if prov in _PROVIDERS_WITHOUT_ENV:
            continue
        output_fn(f"  {prov} → {prov.upper()}_API_KEY (set this env var)")
    # Theme message after Enabled/env, matching the pre-reorder output order.
    if picked_this_run and theme != "ansi":
        output_fn(f"Detected a {theme} terminal — colors tuned to match.")

    # --- readiness gate + agent detection (fresh init on a Python-packaging
    #     project only); otherwise keep today's behavior ---
    detected_roots: list[str] | None = None   # None/[] => write the teaching hint
    run_gate = (not already_initialized
                and is_python_packaging_project(project_root))

    if not run_gate:
        if not sandbox_available():
            output_fn("")
            output_fn("WARNING: bubblewrap (bwrap) not found — imp can't OS-sandbox the agent's")
            output_fn("   shell. Install it for a real filesystem boundary:")
            output_fn("     Fedora:  sudo dnf install bubblewrap")
            output_fn("     Debian:  sudo apt install bubblewrap")
            output_fn("   Until then, set `sandbox.allow_unsandboxed: true` in your")
            output_fn(f"   user config ({user_path}) to run without a boundary,")
            output_fn("   or runs will hard-fail. (You can also re-run init after installing.)")
            output_fn("")
    else:
        # 1. Credential check (offline) over the picked providers.
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

        # 2. Sandbox check.
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

        # 3. Persist the user config before running the agent (so a re-run never
        #    re-asks the questions already answered).
        if wrote_user_config:
            _bootstrap_user_config(
                user_path, providers, theme, allow_unsandboxed=allow_unsandboxed)

        # 4. Run the agent detection.
        output_fn(
            "Inspecting your project's packaging to find its Python import root "
            f"(via {provider_name})…")
        workflow_dir = resolve_workflow_dir(project_root, _DEFAULT_WORKFLOW)
        outcome = detect_pythonpath_via_agent(
            project_root, workflow_dir, provider_name, model_name,
            SandboxConfig(allow_unsandboxed=allow_unsandboxed))
        if isinstance(outcome, DetectionFailed):
            if outcome.kind == "credential":
                output_fn(
                    f"imp reached {provider_name} but the credential was rejected.")
                hint = _CREDENTIAL_HINTS.get(provider_name, "check its credential")
                output_fn(f"{hint[0].upper()}{hint[1:]}, then re-run `imp init`.")
            else:
                output_fn(
                    "imp couldn't finish detecting your Python import root "
                    f"({outcome.detail}).")
                output_fn("Re-run `imp init` once your environment is ready.")
            return
        assert isinstance(outcome, DetectedRoots)
        detected_roots = outcome.roots

    # --- committed project file (idempotent: never overwrite an existing one) ---
    if not already_initialized:
        default_base = _resolve_init_base(
            project_root, git_created=git_created, input_fn=input_fn)
        output_fn(f"Integration branch: {default_base}")
        imp_dir.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(
            _build_project_config(default_base),
            sort_keys=False, default_flow_style=False)
        detect_cmd = detect_verify_fn or detect_test_command
        suggested = detect_cmd(project_root)
        test_block = ""
        if suggested is not None:
            chosen = resolve_test_cmd_choice(suggested, input_fn, output_fn)
            test_block = (
                "\n# Fast test gate: lint + typecheck + fast tests, run after each\n"
                "# implement loop to catch regressions. Empty string opts out.\n"
                "# The slow gate (test_cmd_slow) runs the full suite once per quest\n"
                "# and falls back to test_cmd_fast when unset. Timeouts default to\n"
                "# 120s (fast) / 300s (slow) — override with test_cmd_fast_timeout /\n"
                "# test_cmd_slow_timeout.\n"
                + format_test_cmd_fast_yaml(chosen)
            )
        if detected_roots:
            pythonpath_block = (
                "\n# Worktree-relative import roots prepended to gate/agent\n"
                "# subprocess PYTHONPATH, so an editable-installed package resolves\n"
                "# to the worktree under test, not the stale main checkout.\n"
                + format_pythonpath_yaml(detected_roots)
            )
            output_fn(f"Set pythonpath: {detected_roots} in imp/project.yaml.")
            output_fn(
                "Review your configuration with `imp config`; edit "
                "imp/project.yaml to change it.")
        else:
            pythonpath_block = _pythonpath_hint()
            if run_gate:
                output_fn(
                    "No installable Python package detected — leaving pythonpath "
                    "unset.")
        project_file.write_text(
            body + test_block + pythonpath_block + _facts_hint())
        _write_memory_scaffold(imp_dir)

    # --- bootstrap user config (only if the machine still has none) ---
    if wrote_user_config:
        _bootstrap_user_config(user_path, providers, theme, allow_unsandboxed=False)

    # --- ensure .imp/ is gitignored (control-root state lives there) ---
    project_gi = project_root / ".gitignore"
    existing = project_gi.read_text() if project_gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    if ".imp/" not in existing_lines:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        project_gi.write_text(existing + sep + "\n# imp\n.imp/\n")

    committed = _commit_init_files(project_root)

    output_fn("")
    if already_initialized:
        output_fn("Project already initialized (imp/project.yaml is committed).")
        if wrote_user_config:
            output_fn("Bootstrapped your machine config (providers, theme).")
        else:
            output_fn("Your machine config is already set up — nothing to do.")
    else:
        output_fn("Created:")
        output_fn("  imp/project.yaml")
    output_fn(f"  {user_path}  (your machine: providers, theme)")
    if committed:
        output_fn("Committed imp/project.yaml.")
    output_fn("")
    output_fn("Try: imp new \"Add a dark-mode toggle\"")
