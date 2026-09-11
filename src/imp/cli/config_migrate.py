# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""One-shot startup migrations: the global config file's move into $IMP_HOME, the
project config file's staged rename, the `repo` ownership marker's backfill into
imp-owned state directories, the per-project state directory's move out of the
checkout, and the spike evidence's removal from the artifact refs. Run by the
console-script entrypoint (`imp.__main__:main`), never by `imp.cli.main:main` —
the suite drives the latter in-process, where a migration would rearrange the
developer's own files."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from imp.paths import _slug, display_path, imp_home, project_state_dir


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


# Not an anchored key matcher: `context: {checkpoint_threshold: N}` puts the key
# after `context: {` on its line, and a pre-check that misses it never rewrites
# the file at all. "Live key present" is also exactly "not yet migrated", since
# apply_unset strips by commenting out.
_LIVE_CHECKPOINT_KEY = re.compile(
    r"""^[^#\n]*(?<![\w.-])["']?checkpoint_threshold["']?[ \t]*:""",
    re.MULTILINE,
)


def _has_pending(text: str) -> bool:
    """Whether any transform has work to do — the byte pre-check, before any
    ruamel import or parse."""
    return bool(_LEGACY_OPT_OUT.search(text)) or bool(_LIVE_CHECKPOINT_KEY.search(text))


def rewrite_config_file(path: Path) -> None:
    """Every pending transform for one config file, composed into one candidate
    behind ONE landing gate. verify_readable parses the whole file, so a gate per
    transform makes each abort on the other's un-migrated defect — permanently,
    and whichever runs first."""
    if not path.is_file():
        return
    text = path.read_text()
    if not _has_pending(text):
        return
    # Deferred past the byte pre-check: the post-migration steady state pays no
    # ruamel import and no parse.
    from imp.cli.config_edit import UnsetOutcome, apply_set, apply_unset
    from imp.cli.config_write import land, scratch_seam, verify_readable

    keys = _legacy_keys(text)
    retired = _LIVE_CHECKPOINT_KEY.search(text) is not None
    notices: list[str] = []
    if retired:
        result = apply_unset(text, ("context", "checkpoint_threshold"))
        if result.outcome in (UnsetOutcome.SHARED_LINE, UnsetOutcome.FLOW_BLOCKED):
            # The key stays live, so nothing may land: the reader would reject the
            # whole file. Point at it instead of leaving a bare LoadError.
            print("imp: remove the retired context.checkpoint_threshold setting "
                  f"from {display_path(path)} by hand", file=sys.stderr)
            return
        if result.outcome is UnsetOutcome.COMMENTED:
            text = result.text
            notices.append("imp: removed the retired context.checkpoint_threshold "
                           f"setting from {display_path(path)}")
    for key in keys:
        text = apply_set(text, (key,), False).text
        spelling = "[]" if key == "pythonpath" else '""'
        notices.append(
            f"imp: rewrote {spelling} to false in {display_path(path)} ({key})")
    if not notices:
        return                     # a pre-check over-match: apply_unset said ABSENT
    with scratch_seam(path.parent / f".config-migrate-{os.getpid()}",
                      path.name) as scratch:
        verify_readable(scratch, text)
        land(scratch, path)
    for line in notices:
        print(line, file=sys.stderr)


def _checkout_config(cwd: Path) -> Path | None:
    """The MAIN checkout's imp/config.yaml, resolved through git only when a
    config.yaml at or above `cwd` has pending work — a quest worktree must never
    have its own copy rewritten, and the steady state pays no spawn."""
    from imp.engine.worktree import find_control_root

    nearby = next((p / "imp" / "config.yaml" for p in (cwd, *cwd.parents)
                   if (p / "imp" / "config.yaml").is_file()), None)
    if nearby is None or not _has_pending(nearby.read_text()):
        return None
    return find_control_root(cwd) / "imp" / "config.yaml"


def migrate_config_layers(cwd: Path) -> None:
    """Every pending config-file transform across every layer imp reads. The two
    ~/.imp locations go first: only the checkout needs git to resolve, and
    resolving it can raise outside a work tree — which under the caller's one
    `suppress` would otherwise take the machine-wide files down with it."""
    from imp.providers.paths import global_config_path

    rewrite_config_file(global_config_path())
    for local in sorted((imp_home() / "projects").glob("*/config.yaml")):
        rewrite_config_file(local)
    checkout = _checkout_config(cwd)
    if checkout is not None:
        rewrite_config_file(checkout)


def _state_candidates(state_dir: Path) -> list[Path]:
    """Every project_root a state directory's own records name. The archive is a
    first-class source, not a fallback: terminal quest homes are reaped, so a project
    between quests has records and no live quest at all."""
    roots = [str(json.loads(meta.read_text()).get("project_root") or "")
             for meta in sorted((state_dir / "quests").glob("*/quest.json"))]
    roots += [str(json.loads(rec.read_text()).get("quest_meta", {}).get("project_root") or "")
              for rec in sorted((state_dir / "archive" / "records").glob("*.json"))]
    return [Path(root) for root in roots if root]


def _owner_of(candidate: Path, state_dir: Path) -> Path | None:
    """The checkout a recorded project_root belongs to. None when the candidate cannot
    be trusted — it vanished, its git dir is unresolvable, or it is not itself a
    checkout — which drops that candidate only, never the whole directory."""
    from imp.engine.worktree import PreconditionError, find_control_root

    if not candidate.is_dir():
        return None
    try:
        control = find_control_root(candidate)
    except PreconditionError:
        return None
    if candidate.resolve().is_relative_to((state_dir / "worktrees").resolve()):
        return control
    return control if control == candidate.resolve() else None


def _could_be_minted(dir_name: str, root: Path) -> bool:
    """The exact inverse of the slug walk's mint rule (paths.py:50): `<slug>`, or
    `<slug>-N` for N >= 2. This is what stops a leaked scratch quest — whose recorded
    root lives in the live state dir — from marking that dir for the scratch checkout."""
    base = _slug(root.name)
    if dir_name == base:
        return True
    suffix = dir_name.removeprefix(f"{base}-")
    return (suffix.isdecimal() and int(suffix) >= 2
            and dir_name == f"{base}-{int(suffix)}")


def _derive_owner(state_dir: Path) -> Path | None:
    """The one checkout a marker-less state directory belongs to, or None when its
    surviving candidates do not agree on exactly one."""
    # Deduplicated: one git spawn per DISTINCT candidate, however many records name it.
    owners = {owner for candidate in dict.fromkeys(_state_candidates(state_dir))
              if (owner := _owner_of(candidate, state_dir)) is not None
              and _could_be_minted(state_dir.name, owner)}
    return owners.pop() if len(owners) == 1 else None


def _holds_state(directory: Path) -> bool:
    quests = directory / "quests"
    return ((quests.is_dir() and any(quests.iterdir()))
            or (directory / "archive").is_dir())


def _live_claims(projects: Path) -> set[str]:
    """The owner paths a sibling directory already claims AND holds real state for.
    A marker-only stub is not a claim — it has no quests to orphan — so it never blocks
    the repair; marking the live directory beside it retires the stub in the walk."""
    claims: set[str] = set()
    for directory in sorted(projects.iterdir()):
        with suppress(Exception):   # isolated per sibling, like the sweep itself
            marker = directory / "repo"
            if marker.is_file() and _holds_state(directory):
                claims.add(marker.read_text().strip())
    return claims


def backfill_repo_markers() -> None:
    """Write the missing `repo` ownership marker into every imp-owned state directory
    whose own records determine its owner, so `_checked_override` has something to
    refuse a foreign checkout with. Bounded to `imp_home()/projects/`: a hand-pinned
    $IMP_STATE_DIR is never marked. Each directory is isolated — a malformed record or
    a read-only directory skips that one and the sweep continues. Siblings are read
    once up front, so a marker this pass writes cannot change a later directory's
    outcome."""
    projects = imp_home() / "projects"
    if not projects.is_dir():
        return
    claimed = _live_claims(projects)
    for directory in sorted(projects.iterdir()):
        with suppress(Exception):
            marker = directory / "repo"
            if not directory.is_dir() or marker.exists():
                continue
            owner = _derive_owner(directory)
            if owner is None or str(owner) in claimed:
                continue
            marker.write_text(f"{owner}\n")


def migrate_spike_artifact_refs(cwd: Path) -> None:
    """Drop the spike evidence every artifact ref committed before it moved out of
    `artifacts/`, once per project."""
    from filelock import Timeout

    from imp.engine.worktree import find_control_root, prune_spike_from_artifact_refs

    control_root = find_control_root(cwd)
    marker = project_state_dir(control_root) / "spike-refs-pruned"
    if marker.exists():
        return
    try:
        pruned = prune_spike_from_artifact_refs(control_root)
    except Timeout:
        return                    # a live harvest holds the lock; retry next invocation
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    if pruned:
        noun = "ref" if pruned == 1 else "refs"
        print(f"imp: pruned spike evidence from {pruned} artifact {noun}; "
              "run `git gc` to reclaim the space.", file=sys.stderr)


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
        migrate_config_layers(Path.cwd())
    # Before the state move: that step mints through project_state_dir itself, so
    # backfilling first means it resolves against repaired markers instead of racing them.
    with suppress(Exception):
        backfill_repo_markers()
    # Lazy: state_migrate imports _git_toplevel from here, so a module-scope import
    # would be circular.
    from imp.cli.state_migrate import migrate_project_state

    migrate_project_state(Path.cwd())
    # Last: it resolves the state directory, so it must see its final location.
    with suppress(Exception):
        migrate_spike_artifact_refs(Path.cwd())
