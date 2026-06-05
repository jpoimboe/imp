"""bash tool (spec §6.6 + §6.15).

Runs a shell command via anyio.run_process in its own session (POSIX), so the
process group can be SIGTERM/SIGKILL'd from the runner's interrupt path. The
process is registered on ctx.running_processes for the duration; the runner's
terminate_running_processes() walks the registry on interrupt.

No FS permission gate (shell access is total at OS-user privilege; see spec
§6.7 "bash bypasses FS permissions").
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
from dataclasses import dataclass
from subprocess import PIPE

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext, kill_process_group
from imp.agents.tools.limits import OUTPUT_TRUNCATION_MARKER, TOOL_OUTPUT_CAP_BYTES
from imp.engine.env import engine_env, overlay_engine_env

# Rewrite a LEADING `cd /project` (optionally `/subpath`, optionally single- or
# double-quoted) to the real project root. `/project` is a virtual FS-tool
# namespace; in bash the cwd already IS the root, so the reflexive `cd /project …`
# would fail. Word-boundary match: `/project` followed by `/`, end, or a non-word
# char — never `/projectfoo` or `/project_x`, and never a `/project` substring
# later in the command. Group 1 includes the optional opening quote so the
# substitution preserves it; the closing quote is never in the match (it lies
# past `/project`), so it passes through untouched. Inside a regex char class,
# `\'` is an escaped quote = literal `'`, so the class matches `"` or `'`.
_LEADING_CD_PROJECT = re.compile(r'^(\s*cd\s+["\']?)/project(?![A-Za-z0-9_])')


def _rewrite_leading_cd_project(command: str, project_root: str) -> str:
    return _LEADING_CD_PROJECT.sub(lambda m: m.group(1) + project_root, command, count=1)


@dataclass
class _Counter:
    """Shared combined-output state for the two drain tasks. `total` is the
    running byte count across BOTH sinks; `capped` flips once it crosses the cap;
    `which` records the stream that crossed it (so the marker lands on it)."""

    total: int = 0
    capped: bool = False
    which: str = "stdout"


@imp_tool(
    name="bash",
    description=(
        "Run a shell command. Returns JSON {stdout, stderr, returncode}. "
        "No permission gate (shell access is total at OS-user level). "
        "Output is capped at 50 KB. Interruptible: a runner interrupt sends "
        "SIGTERM to the process group; 4 s grace; then SIGKILL."
    ),
)
async def bash(ctx: ToolContext, command: str, cwd: str | None = None) -> str:
    command = _rewrite_leading_cd_project(command, str(ctx.project_root))
    cwd_path = cwd or str(ctx.project_root)
    env = overlay_engine_env(
        os.environ,
        engine_env(project_root=ctx.project_root,
                   change_id=ctx.change_context.id if ctx.change_context else "",
                   change_slug=ctx.change_context.slug if ctx.change_context else "",
                   change_title=ctx.change_context.title if ctx.change_context else "",
                   lint_command=ctx.lint_command,
                   test_command=ctx.test_command),
    )
    # POSIX: start_new_session=True makes the child a new session leader so
    # SIGTERM to the PG affects the whole tree. Windows: this kwarg is ignored
    # and signal handling differs — out of V1 scope (we ship POSIX-first).
    kwargs: dict[str, object] = {}
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True

    # anyio.open_process accepts stdlib subprocess constants for stdin/stdout/
    # stderr; PIPE is the documented way to capture both streams.
    proc = await anyio.open_process(
        ["sh", "-c", command],
        stdin=None,
        stdout=PIPE,
        stderr=PIPE,
        cwd=cwd_path,
        env=env,
        **kwargs,  # type: ignore[arg-type]
    )
    # M1 invariant: every proc registered here is a setsid session leader
    # (start_new_session above), so pgid == pid and terminate_running_processes /
    # kill_process_group can os.killpg(pid, …) without os.getpgid. If a future
    # caller ever registers a NON-setsid proc, revisit kill_process_group.
    ctx.running_processes.append(proc)
    cap = TOOL_OUTPUT_CAP_BYTES
    # Combined running counter across BOTH sinks: counting separately would let
    # the total reach 2x the cap. When the combined total crosses the cap and the
    # process is still producing, kill the GROUP (cleanup) and stop draining.
    # N5: peak buffered bytes is cap + up to one in-flight chunk PER stream (both
    # drains can append before either observes `capped`), i.e. <= ~cap + 2*READ
    # chunk (~50 KB + ~128 KB). Bounded and expected — that is the "slop" the
    # final stdout[:cap] slice trims off before returning.
    # Single mutable record shared by both drains. Typed fields (not a bag of
    # `object`) so the `int(...)` arithmetic below stays mypy-clean.
    counter = _Counter()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _drain(stream: object, sink: list[bytes], name: str) -> None:
        if stream is None:
            return
        async for chunk in stream:  # type: ignore[attr-defined]
            if counter.capped:
                return
            sink.append(chunk)
            counter.total += len(chunk)
            if counter.total >= cap:
                counter.capped = True
                counter.which = name           # S2: which stream crossed the cap
                await kill_process_group(proc, signal.SIGKILL)
                return

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_drain, proc.stdout, stdout_chunks, "stdout")
            tg.start_soon(_drain, proc.stderr, stderr_chunks, "stderr")
            await proc.wait()
    finally:
        with contextlib.suppress(ValueError):
            ctx.running_processes.remove(proc)

    stdout = b"".join(stdout_chunks).decode("utf-8", "replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    if counter.capped:
        # Head-only: bound BOTH decoded streams to the cap and append the marker to
        # the one that actually crossed it (S2: a stderr-dominant flood must not
        # leave a "truncated" marker on an empty stdout). Report returncode 0 — a
        # cap-truncation is a NORMAL result; the SIGKILL we issued was cleanup and
        # must NOT read as `tool_killed` (the provider keys that on returncode < 0;
        # see anthropic.py:704).
        stdout = stdout[:cap]
        stderr = stderr[:cap]
        if counter.which == "stderr":
            stderr += OUTPUT_TRUNCATION_MARKER
        else:
            stdout += OUTPUT_TRUNCATION_MARKER
        returncode = 0
    else:
        returncode = proc.returncode if proc.returncode is not None else -signal.SIGKILL

    return json.dumps({
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
    })
