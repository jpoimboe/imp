"""One-shot startup migrations: the global config file's move into $IMP_HOME, the
project config file's staged rename, and the per-project state directory's move out
of the checkout. Run by the console-script entrypoint (`imp.__main__:main`), never
by `imp.cli.main:main` — the suite drives the latter in-process, where a migration
would rearrange the developer's own files."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from imp.paths import display_path


def migrate_global_config(legacy: Path, new: Path) -> None:
    """Move a pre-IMP_HOME global config into $IMP_HOME, once."""
    if not legacy.is_file() or new.exists():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy), str(new))  # not os.replace: the two may differ in fs
    with suppress(OSError):
        legacy.parent.rmdir()
    print(f"imp: moved {display_path(legacy)} to {display_path(new)}",
          file=sys.stderr)


def _git_toplevel(cwd: Path) -> Path | None:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def migrate_project_config(cwd: Path) -> None:
    """Stage `imp/project.yaml` -> `imp/config.yaml` in the main checkout, once."""
    from imp.engine.worktree import find_control_root
    from imp.providers.paths import project_rename_pending

    # Pure-filesystem pre-check: an already-migrated repo is the permanent steady
    # state, and must not pay a git spawn on every imp invocation.
    if not any(project_rename_pending(p) for p in (cwd, *cwd.parents)):
        return
    control = find_control_root(cwd)
    if not project_rename_pending(control):
        return
    # In a linked worktree the toplevel is the worktree and the control root the
    # main checkout, so this inequality IS the mid-run guard.
    if _git_toplevel(cwd) != control:
        return
    subprocess.run(["git", "mv", "imp/project.yaml", "imp/config.yaml"],
                   cwd=control, capture_output=True, text=True, check=True)
    print("imp: renamed imp/project.yaml to imp/config.yaml (staged) — "
          "commit it to finish the move", file=sys.stderr)


# Anchored at column 0 and paired per key: `apply_set` can only fix a TOP-LEVEL
# key, and each key is matched against ITS own legacy spelling only, so the
# announced token below is true by construction.
_LEGACY_OPT_OUT = re.compile(
    r"^(?:(test_cmd_fast|test_cmd_slow|worktree_setup)[ \t]*:[ \t]*(?:\"\"|'')"
    r"|(pythonpath)[ \t]*:[ \t]*\[[ \t]*\])[ \t]*(?:#.*)?$",
    re.MULTILINE,
)


def _legacy_keys(text: str) -> list[str]:
    """The top-level keys written with the pre-`false` opt-out spelling, in file
    order."""
    found: list[str] = []
    for match in _LEGACY_OPT_OUT.finditer(text):
        key = match.group(1) or match.group(2)
        if key not in found:
            found.append(key)
    return found


def rewrite_opt_out_spelling(path: Path) -> None:
    """Rewrite one config file's `""` / `[]` opt-outs to `false`, comments intact.
    Every key in a single pass: verifying a per-key write would hand the rejecting
    reader a file still carrying a sibling legacy spelling, aborting the rewrite."""
    if not path.is_file():
        return
    text = path.read_text()
    keys = _legacy_keys(text)
    if not keys:
        return
    # Deferred past the byte pre-check: the post-migration steady state pays no
    # ruamel import and no parse.
    from imp.cli.config_edit import apply_set
    from imp.cli.config_write import land, scratch_seam, verify_readable

    for key in keys:
        text = apply_set(text, (key,), False).text
    with scratch_seam(path.parent / f".config-migrate-{os.getpid()}",
                      path.name) as scratch:
        verify_readable(scratch, text)
        land(scratch, path)
    for key in keys:
        spelling = "[]" if key == "pythonpath" else '""'
        print(f"imp: rewrote {spelling} to false in {display_path(path)} ({key})",
              file=sys.stderr)


def _checkout_config(cwd: Path) -> Path | None:
    """The MAIN checkout's imp/config.yaml, resolved through git only when a
    config.yaml at or above `cwd` carries a legacy spelling — a quest worktree
    must never have its own copy rewritten, and the steady state pays no spawn."""
    from imp.engine.worktree import find_control_root

    nearby = next((p / "imp" / "config.yaml" for p in (cwd, *cwd.parents)
                   if (p / "imp" / "config.yaml").is_file()), None)
    if nearby is None or not _legacy_keys(nearby.read_text()):
        return None
    return find_control_root(cwd) / "imp" / "config.yaml"


def migrate_opt_out_spelling(cwd: Path) -> None:
    """`""` / `[]` -> `false` across every config layer imp reads. The two ~/.imp
    locations go first: only the checkout needs git to resolve, and resolving it
    can raise outside a work tree — which under the caller's one `suppress` would
    otherwise take the machine-wide files down with it."""
    from imp.paths import imp_home
    from imp.providers.paths import global_config_path

    rewrite_opt_out_spelling(global_config_path())
    for local in sorted((imp_home() / "projects").glob("*/config.yaml")):
        rewrite_opt_out_spelling(local)
    checkout = _checkout_config(cwd)
    if checkout is not None:
        rewrite_opt_out_spelling(checkout)


def run_startup_migrations() -> None:
    """Best-effort per config step: one that cannot run is retried on the next
    invocation and must never be the reason a command dies. The state move is
    deliberately outside that promise — imp running on half-moved state is worse
    than imp not running."""
    with suppress(Exception):
        from imp.providers.paths import (
            global_config_path,
            legacy_global_config_path,
        )
        migrate_global_config(legacy_global_config_path(), global_config_path())
    with suppress(Exception):
        migrate_project_config(Path.cwd())
    with suppress(Exception):
        migrate_opt_out_spelling(Path.cwd())
    # Lazy: state_migrate imports _git_toplevel from here, so a module-scope import
    # would be circular.
    from imp.cli.state_migrate import migrate_project_state

    migrate_project_state(Path.cwd())
