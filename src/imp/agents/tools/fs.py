"""read_file, write_file, edit_file (spec §6.6).

Permissions enforced inline (no backend layer). Paths are worktree-RELATIVE and
resolved via ToolContext.resolve_path, which enforces worktree containment.
"""

from __future__ import annotations

import anyio

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import (
    FILE_READ_CAP_BYTES,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    read_bytes_capped,
    read_text_capped,
    tool_checkpoint,
)
from imp.agents.tools.lines import file_is_cap_truncated, split_lines, truncate_line


@imp_tool(
    name="read_file",
    description=(
        "Read a file's contents (bounded — output is capped at 50 KB). "
        "Paths are relative to the project root (e.g. src/foo.py)."
    ),
)
async def read_file(ctx: ToolContext, path: str, *, offset: int = 1, limit: int = 2000) -> str:
    if type(offset) is not int or offset < 1:
        raise ValueError(f"offset must be an integer >= 1, got {offset!r}")
    if type(limit) is not int or limit < 1:
        raise ValueError(f"limit must be an integer >= 1, got {limit!r}")
    ctx.check_fs_permission("read", path)
    resolved = ctx.resolve_path(path)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S
    try:
        text, read_truncated = await read_text_capped(
            resolved,
            coordinator=coordinator,
            deadline=deadline,
            cap_bytes=FILE_READ_CAP_BYTES,
        )
    except FileNotFoundError:
        raise FileNotFoundError(f"no such file: {path}") from None
    except IsADirectoryError:
        raise IsADirectoryError(f"is a directory: {path}") from None
    if not text:
        return "[empty file]"  # wins regardless of offset — never past-EOF
    # st_size sampled AFTER the read (growth during the read must classify as
    # cap-truncated; a before-read sample would fabricate a final line).
    cap_truncated = file_is_cap_truncated(read_truncated, resolved)
    # Seam: the bounded read checkpoints per chunk, but the split/number/render
    # of up to 5 MB below is one synchronous stretch — checkpoint once between.
    await tool_checkpoint(coordinator, deadline)
    lines = split_lines(text, cap_truncated=cap_truncated)
    readable = len(lines)

    if offset > readable:
        if cap_truncated:
            return (f"[offset {offset} is past the readable window "
                    f"({readable} lines within the first 5 MB; total unknown)]")
        return f"[offset {offset} is past the end of the file ({readable} lines)]"

    rendered: list[str] = []
    rendered_len = 0
    last = offset - 1
    for lineno in range(offset, min(offset + limit - 1, readable) + 1):
        # Per-line cap applies to CONTENT only; the {lineno:>6}\t prefix does
        # count toward the 50 KB budget below (it is rendered output).
        entry = f"{lineno:>6}\t{truncate_line(lines[lineno - 1])}"
        rendered.append(entry)
        rendered_len += len(entry) + 1  # +1 = the join newline (grep's accounting)
        last = lineno
        if rendered_len >= TOOL_OUTPUT_CAP_BYTES:
            break  # budget-crossing line stays rendered IN FULL (matches grep)

    # Three cutoffs (limit lines / 50 KB budget / 5 MB read ceiling), one
    # resumable notice. Skeleton strings are NORMATIVE — tests assert them
    # verbatim; wording changes go through the spec.
    notice: str | None = None
    if cap_truncated:
        # EVERY notice for a cap-truncated file uses readable-window
        # vocabulary and never claims a total — regardless of which cutoff
        # ended the window. A window ending at the readable edge must NOT end
        # silently: quiet endings are the signature of a complete file.
        if last < readable:
            notice = (f"[showing lines {offset}-{last} of the first 5 MB "
                      f"({readable} lines readable; total unknown); "
                      f"continue with offset={last + 1}]")
        else:
            notice = (f"[showing lines {offset}-{last}; end of the readable window "
                      f"(first 5 MB) — the file continues but is not readable via "
                      f"read_file; total unknown]")
    elif last < readable:
        notice = (f"[showing lines {offset}-{last} of {readable}; "
                  f"continue with offset={last + 1}]")
    # else: a complete file's window reached (or overshot) EOF — ends silently.

    body = "\n".join(rendered)
    return f"{body}\n{notice}" if notice else body


@imp_tool(name="write_file", description="Write (overwrite) a file. Creates parent dirs as needed.")
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    ctx.check_fs_permission("write", path)
    resolved = ctx.resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    return f"wrote {len(content)} bytes to {path}"


@imp_tool(
    name="edit_file",
    description=(
        "Replace a single unique occurrence of `old` with `new` in `path`. "
        "Raises if `old` not found or not unique."
    ),
)
async def edit_file(ctx: ToolContext, path: str, old: str, new: str, *,
                    replace_all: bool = False) -> str:
    if old == "":
        # "abc".count("") == len+1, so an empty old is never "absent" — and
        # under replace_all, body.replace("", new) would insert `new` between
        # EVERY character of the file. (Previously only the accidental
        # count("") > 1 uniqueness error blocked this, with a misleading
        # message; replace_all=True removes exactly that accident.)
        raise ValueError("old must not be empty")
    ctx.check_fs_permission("read", path)
    ctx.check_fs_permission("write", path)
    resolved = ctx.resolve_path(path)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S
    # Bounded via the READ itself, not a stat-then-read guard (a file that
    # grows between stat and read — an appending log — would reopen the
    # 3 GB-single-line incident).
    try:
        raw, read_truncated = await read_bytes_capped(
            resolved, coordinator=coordinator, deadline=deadline)
    except FileNotFoundError:
        raise FileNotFoundError(f"no such file: {path}") from None
    except IsADirectoryError:
        raise IsADirectoryError(f"is a directory: {path}") from None
    # Refusal order pinned: SIZE before strict decode. A >5 MB valid-UTF-8
    # file has its byte cap land mid-multibyte-sequence; decode-first would
    # blame encoding for a size problem. file_is_cap_truncated also refuses a
    # file that vanished after a truncated read (it may be larger than what
    # we read), and admits an exactly-5 MB file (read completely — editable).
    if file_is_cap_truncated(read_truncated, resolved):
        raise ValueError(
            f"{path} is larger than 5 MB; edit_file refuses to rewrite files "
            f"past the read cap — use bash instead")
    try:
        # STRICT decode. errors="replace" is fine for display-only tools but
        # catastrophic in a read-modify-WRITE tool: every invalid byte
        # anywhere in the file would become U+FFFD in body, and the
        # write-back rewrites the whole file.
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"{path} is not valid UTF-8; edit_file refuses to rewrite it "
            f"(the write-back would corrupt non-UTF-8 bytes) — use bash instead"
        ) from None
    count = body.count(old)
    if count == 0:
        raise ValueError(f"{old!r} not found in {path!r}")
    if not replace_all and count > 1:
        raise ValueError(f"{old!r} not unique in {path!r} (found {count} occurrences)")
    edited = body.replace(old, new) if replace_all else body.replace(old, new, 1)
    # Bytes-faithful write-back: encode and write BYTES (no platform newline
    # translation — the old read_text() path mangled CRLF files).
    resolved.write_bytes(edited.encode("utf-8"))
    if replace_all:
        return f"replaced {count} occurrences in {path}"
    return f"replaced 1 occurrence in {path}"
