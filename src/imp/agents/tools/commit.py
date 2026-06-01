"""commit tool (spec §12).

Stages the given paths and creates (or --amends) a git commit, auto-appending a
`Change: <slug>` trailer so every commit traces back to its Change. Like bash,
it shells out to git in the worktree and is NOT gated by fs_permissions — the
per-agent `tools:` list is the sole gate (spec §12: "commit is not an
fs-permission boundary"). The trailer is idempotent on --amend, so repeated
review-fix amends never duplicate it.
"""

from __future__ import annotations

import subprocess
from typing import Any

from imp.agents.tools import imp_tool


def _git(cwd: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, input=stdin,
        capture_output=True, text=True,
    )


@imp_tool(
    name="commit",
    description=(
        "Stage the given paths and create a git commit with your message. "
        "Auto-appends a `Change: <slug>` trailer (idempotent). Pass amend=true "
        "to fold review fixes into the current commit instead of making a new "
        "one. paths are /project-relative (e.g. /project/imp/specs/x.md). "
        "Pass fixup=<commit-ish> to record a fix to an earlier (buried) commit; "
        "it is folded by autosquash before review."
    ),
)
def commit(ctx: Any, paths: list[str], message: str, amend: bool = False,
           fixup: str | None = None) -> str:
    if ctx.change_context is None or not ctx.change_context.slug:
        raise RuntimeError("commit requires an active Change context")
    slug = ctx.change_context.slug
    cwd = str(ctx.project_root)

    if amend and fixup:
        raise ValueError("commit: pass either amend or fixup, not both")

    if fixup is not None and not paths:
        raise ValueError("commit: fixup requires at least one path")

    # Resolve virtual /project paths to real worktree paths. No permission gate:
    # commit is not an fs boundary (spec §12). resolve_path RAISES on a non
    # /project|/scratch prefix and RETURNS None for /scratch, so normalize both
    # to one "outside /project" error.
    resolved: list[str] = []
    for p in paths:
        try:
            rp = ctx.resolve_path(p)
        except ValueError:
            rp = None
        if rp is None:
            raise ValueError(f"commit: path {p!r} is outside /project")
        resolved.append(str(rp))

    if resolved:
        add = _git(cwd, "add", "--", *resolved)
        if add.returncode != 0:
            raise ValueError(f"git add failed: {add.stderr.strip()}")

    if fixup is not None:
        # Fixup commit: no trailer (autosquash folds it into <fixup>, which
        # already carries the trailer). git supplies the `fixup! <subject>` msg.
        done = _git(cwd, "commit", f"--fixup={fixup}")
        if done.returncode != 0:
            raise ValueError(
                f"git commit --fixup failed: {done.stderr.strip() or done.stdout.strip()}"
            )
        head = _git(cwd, "rev-parse", "--short", "HEAD").stdout.strip()
        return f"fixup {head} -> {fixup}: {len(resolved)} path(s); fold via autosquash"

    # Build the final message: append exactly one `Change:` trailer to the raw
    # agent message. Idempotency across amends comes from `--amend -m` replacing
    # the whole message (a fresh single-trailer build each time), NOT from
    # --if-exists — which is belt-and-suspenders for the rare case where the
    # agent itself put a `Change:` line in `message`.
    trailered = _git(cwd, "interpret-trailers", "--if-exists", "doNothing",
                     "--trailer", f"Change: {slug}", stdin=message)
    final_message = (
        trailered.stdout if trailered.returncode == 0
        else f"{message.rstrip()}\n\nChange: {slug}\n"
    )

    commit_args = ["commit", "-m", final_message]
    if amend:
        commit_args.insert(1, "--amend")
    done = _git(cwd, *commit_args)
    if done.returncode != 0:
        raise ValueError(
            f"git commit failed: {done.stderr.strip() or done.stdout.strip()}"
        )

    head = _git(cwd, "rev-parse", "--short", "HEAD").stdout.strip()
    verb = "amended" if amend else "committed"
    return f"{verb} {head}: {len(resolved)} path(s) staged, trailer Change: {slug}"
