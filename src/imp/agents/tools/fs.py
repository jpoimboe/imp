"""read_file, write_file, edit_file (spec §6.6).

Every call funnels through ToolContext.authorize_fs, which bounds the path by the
sandbox mirror like `bash` (allow-all when unsandboxed).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from pathlib import Path

import anyio

import imp.agents.tools.image as image_tools
from imp.agents.runner import ContentPart, TextPart
from imp.agents.tools import imp_tool
from imp.agents.tools.context import ReadRecord, ToolContext
from imp.agents.tools.limits import (
    FILE_READ_CAP_BYTES,
    TOOL_OUTPUT_CAP_BYTES,
    TOOL_WALLCLOCK_TIMEOUT_S,
    read_bytes_capped,
    tool_checkpoint,
)
from imp.agents.tools.lines import file_is_cap_truncated, split_lines, truncate_line


@imp_tool(
    name="read_file",
    description=(
        "Read a file — text or image (PNG/JPEG/GIF/WebP detected by content, "
        "regardless of extension). Images return a viewable image part plus a "
        "short caption; text files return 1-based line numbers (`cat -n` style). "
        "For text: `offset` (default 1) is the first line to show; `limit` "
        "(default 2000) caps the lines returned; a trailing [notice] says how "
        "to continue when more lines remain (output is also bounded at 50 KB, "
        "and only the first 5 MB of a file is readable). Paths are relative to "
        "the project root (e.g. src/foo.py); absolute and out-of-worktree paths "
        "are also accepted, bounded by the sandbox like bash. IMPORTANT: the "
        "line-number prefix is display-only — "
        "never copy it into any write path (edit_file's `old`, write_file's "
        "`content`, or a bash heredoc); doing so writes numbered garbage and "
        "nothing errors. Two more display artifacts are not file content: "
        "the `…[truncated]` suffix on lines over 2000 chars, and U+FFFD "
        "replacement characters standing in for invalid bytes — pasting "
        "either into edit_file/write_file corrupts or never matches. The "
        "opposite trap on CRLF files: every line ends in an invisible "
        "carriage return that IS file content — preserve it when copying "
        "multi-line text into edit_file's `old`/`new` or write_file."
    ),
    read_only=True,
)
async def read_file(
    ctx: ToolContext, path: str, *, offset: int = 1, limit: int = 2000
) -> str | list[ContentPart]:
    if type(offset) is not int or offset < 1:
        raise ValueError(f"offset must be an integer >= 1, got {offset!r}")
    if type(limit) is not int or limit < 1:
        raise ValueError(f"limit must be an integer >= 1, got {limit!r}")
    resolved = ctx.authorize_fs(("read",), path)
    coordinator = getattr(ctx, "coordinator", None)
    deadline = anyio.current_time() + TOOL_WALLCLOCK_TIMEOUT_S
    try:
        data, read_truncated = await read_bytes_capped(
            resolved,
            coordinator=coordinator,
            deadline=deadline,
            cap_bytes=FILE_READ_CAP_BYTES,
        )
    except FileNotFoundError:
        raise FileNotFoundError(f"no such file: {path}") from None
    except IsADirectoryError:
        raise IsADirectoryError(f"is a directory: {path}") from None
    if image_tools.is_image(data):
        return await _read_image(
            ctx, resolved, path, data, read_truncated,
            coordinator=coordinator, deadline=deadline)
    # st_size sampled AFTER the read (growth during the read must classify as
    # cap-truncated; a before-read sample would fabricate a final line).
    cap_truncated = file_is_cap_truncated(read_truncated, resolved)
    sha = hashlib.sha256(data).hexdigest()
    # Skip dedup AND recording (but still return the window) if the file vanished
    # between the read and this stat — the parallel-shared-tree race
    # file_is_cap_truncated already tolerates. An unguarded stat would turn a
    # successful read into a FileNotFoundError; leaving read_state without a
    # record means a later write_file refuses with "hasn't been read this step",
    # which is correct.
    record: ReadRecord | None = None
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None:
        prior = ctx.read_state.get(resolved)
        # Same version == BOTH mtime AND sha equal. On a match the prior record's
        # rendered map is valid and accumulates; any mismatch replaces it fresh.
        if (prior is not None and prior.mtime == mtime
                and prior.content_sha256 == sha):
            if (offset, limit) in prior.rendered:
                first, last = prior.rendered[(offset, limit)]
                return f"[unchanged since you read it at lines {first}-{last} above]"
            record = prior
        else:
            record = ReadRecord(
                mtime=mtime, content_sha256=sha, full=not cap_truncated)
        ctx.read_state[resolved] = record
    text = data.decode("utf-8", "replace")
    if not text:
        return "[empty file]"  # wins regardless of offset — never past-EOF
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

    # Remember the TRUE rendered span (offset..last) for this exact range, so an
    # identical re-read of this unchanged version stubs above. record is None
    # only when the file vanished before stat (dedup + recording both skipped).
    if record is not None:
        record.rendered[(offset, limit)] = (offset, last)
    body = "\n".join(rendered)
    return f"{body}\n{notice}" if notice else body


def _media_label(media_type: str) -> str:
    return media_type.removeprefix("image/").upper()


async def _read_image(
    ctx: ToolContext, resolved: Path, path: str, data: bytes, read_truncated: bool,
    *, coordinator: object | None, deadline: float,
) -> str | list[ContentPart]:
    """Re-read to the image cap if the text-cap read truncated (failing loudly if
    still over cap). For non-vision models returns a text placeholder; for vision
    models stores the image as a blob and returns the summary plus image part."""
    if read_truncated:
        data, img_truncated = await read_bytes_capped(
            resolved, coordinator=coordinator, deadline=deadline,
            cap_bytes=image_tools.IMAGE_READ_CAP_BYTES)
        if img_truncated:
            cap_mb = image_tools.IMAGE_READ_CAP_BYTES // 1_000_000
            raise ValueError(
                f"{path} is larger than the ~{cap_mb} MB image read cap; "
                f"read_file will not truncate binary image data")
    await tool_checkpoint(coordinator, deadline)
    with contextlib.suppress(OSError):
        ctx.read_state[resolved] = ReadRecord(
            mtime=resolved.stat().st_mtime,
            content_sha256=hashlib.sha256(data).hexdigest(),
            full=True,
        )
    from imp.providers.catalog import model_supports_vision

    if not model_supports_vision(ctx.provider_name, ctx.model_name):
        media_type, width, height = image_tools.probe_image(data)
        kb = max(1, round(len(data) / 1024))
        return (
            f"[image: {Path(path).name} — {width}×{height} "
            f"{_media_label(media_type)}, {kb} KB. "
            f"The active model can't display images.]")
    if ctx.change_home is None:
        raise ValueError(
            f"cannot read image {path}: no change home is set for blob storage")
    part = image_tools.store_image(data, ctx.change_home / "blobs")
    summary = TextPart(
        text=f"{Path(path).name} — {part.width}×{part.height} {_media_label(part.media_type)}")
    return [summary, part]


def _hash_file_capped(resolved: Path) -> str | None:
    """sha256 over the first FILE_READ_CAP_BYTES bytes of `resolved`; None if the
    file is larger than the cap (it cannot match a full read's hash, so the
    content-equality fast-path must fail). Bounded: reads at most cap+1 bytes."""
    with open(resolved, "rb") as fh:
        data = fh.read(FILE_READ_CAP_BYTES + 1)
    if len(data) > FILE_READ_CAP_BYTES:
        return None
    return hashlib.sha256(data).hexdigest()


def _gate_write(ctx: ToolContext, resolved: Path, display_path: str) -> None:
    """Read-before-write gate. Raises ValueError on a blind or stale overwrite
    of an existing file; returns silently when the write may proceed."""
    if not resolved.exists():
        return
    record = ctx.read_state.get(resolved)
    if record is None:
        raise ValueError(
            f"{display_path} hasn't been read this step — read_file it before "
            f"overwriting (or use edit_file to change part of it)."
        )
    try:
        current_mtime = resolved.stat().st_mtime
    except OSError:
        return  # vanished between exists() and stat() — treat as new-file write
    if current_mtime > record.mtime:
        # False-positive guard: a touch / sync / formatter that rewrote
        # byte-identical content bumped mtime but not content. Only a FULL read
        # has a hash over the whole file, so only it may take this fast-path.
        if record.full and _hash_file_capped(resolved) == record.content_sha256:
            return
        raise ValueError(
            f"{display_path} changed since you read it — read it again before "
            f"overwriting."
        )


@imp_tool(
    name="write_file",
    description=(
        "Write (overwrite) a file. Creates parent dirs as needed. Absolute and "
        "out-of-worktree paths are accepted, bounded by the sandbox like bash. "
        "`content` "
        "must be file-faithful text — never read_file's numbered display "
        "output (the `{lineno}<TAB>` prefixes would be written literally)."
    ),
)
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    resolved = ctx.authorize_fs(("write",), path)
    _gate_write(ctx, resolved, path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    data = resolved.read_bytes()
    ctx.read_state[resolved] = ReadRecord(
        mtime=resolved.stat().st_mtime,
        content_sha256=hashlib.sha256(data).hexdigest(),
        full=True,
    )
    return f"wrote {len(content)} bytes to {path}"


def _short_repr(s: str, cap: int = 200) -> str:
    """repr capped for error texts. A multi-KB `old` would otherwise be
    echoed in full into the transcript — pure redundancy, since the failing
    value is already in the agent's context as the call it just made. The
    escaped HEAD is what identifies the edit (whitespace mistakes are the
    top not-found cause); the disclosed length tells the rest."""
    if len(s) <= cap:
        return repr(s)
    return f"{s[:cap]!r}… [{len(s)} chars total]"


def _apply_edit(
    body: str, old: str, new: str, path: str, replace_all: bool, *, label: str = "",
) -> tuple[str, int]:
    """Count/find/replace `old`->`new` in an in-memory `body`. Returns the edited
    body and the occurrence count. `label` prefixes error messages (empty for the
    single-edit path -> today's exact text; `edit #N: ` for a batch item). Assumes
    `old` is non-empty — callers validate that before invoking."""
    count = body.count(old)
    if count == 0:
        raise ValueError(f"{label}{_short_repr(old)} not found in {path!r}")
    if not replace_all and count > 1:
        raise ValueError(f"{label}{_short_repr(old)} not unique in {path!r} "
                         f"(found {count} occurrences)")
    edited = body.replace(old, new) if replace_all else body.replace(old, new, 1)
    return edited, count


def _validate_batch(edits: object) -> None:
    """Structural + empty-`old` validation for a batch, run BEFORE any file read
    so a malformed batch never touches disk. Type/shape errors here are the only
    ones raised pre-read; count/find/uniqueness are checked per edit during
    sequential application (still before the single write)."""
    if not isinstance(edits, list):
        raise ValueError("edits must be a list of {old, new, replace_all?} dicts")
    if not edits:
        raise ValueError("edits must not be empty")
    for i, item in enumerate(edits, 1):
        label = f"edit #{i}: "
        if not isinstance(item, dict):
            raise ValueError(f"{label}each edit must be a dict with keys "
                             f"old, new, optional replace_all")
        if "old" not in item or "new" not in item:
            raise ValueError(f"{label}missing required key 'old' or 'new'")
        if not isinstance(item["old"], str) or not isinstance(item["new"], str):
            raise ValueError(f"{label}old and new must be strings")
        if item["old"] == "":
            raise ValueError(f"{label}old must not be empty")
        if not isinstance(item.get("replace_all", False), bool):
            raise ValueError(f"{label}replace_all must be a bool")


def _atomic_write_bytes(resolved: Path, data: bytes, *, display_path: str) -> None:
    """Atomic write-back: temp file + os.replace.

    Sibling implementation: engine/state.py State.save. The differences are
    DELIBERATE, not drift: state.save fsyncs (state.json has no git recovery;
    here durability is waived — the repo recovers) and skips the mode copy
    (no exec bit to preserve on state.json). The BaseException cleanup net is
    shared. Two load-bearing details:
    (1) the temp file is created in the TARGET's directory — os.replace
    across filesystems raises EXDEV, and /tmp is routinely a different mount
    under the project's sandbox reality; (2) the original's permission bits
    are copied onto the temp file BEFORE the replace — mkstemp creates 0600,
    and a bare replace silently strips a script's exec bit. Ownership/xattrs/
    ACLs are waived as accepted. Symlinks are safe by construction:
    resolve_path already resolved, so the replace targets the link TARGET.
    The read-modify-write race (the file changing between the bounded read
    and this replace — real in the parallel-sessions shared tree) is
    accepted, not closed: last-writer-wins, same as before; compare-and-refuse
    on mtime was considered and dropped as false-positive-prone. Durability is
    waived — no fsync before the replace; power-loss may revert/empty the file
    on some filesystems (git is the recovery path). Hard links are waived: the
    replace switches inodes."""
    mode = stat.S_IMODE(resolved.stat().st_mode)
    try:
        fd, tmp_name = tempfile.mkstemp(dir=resolved.parent)
    except PermissionError:
        raise PermissionError(
            f"directory of {display_path} is read-only for this step; edit_file's "
            f"atomic write-back creates a temp file beside the target"
        ) from None
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, resolved)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@imp_tool(
    name="edit_file",
    description=(
        "Replace `old` with `new` in `path`. Absolute and out-of-worktree "
        "paths are accepted, bounded by the sandbox like bash. Default: `old` "
        "must occur "
        "exactly once (raises if absent or not unique). With "
        "`replace_all=true`: replaces every occurrence (raises only if "
        "absent). Refuses files larger than 5 MB and files that are not "
        "valid UTF-8 — use bash for those (a non-UTF-8 file LOOKS editable; "
        "retrying edit_file on it will keep failing). `old` must be "
        "file-faithful text — never read_file/grep display output with its "
        "line-number prefixes, `…[truncated]` suffixes, or U+FFFD "
        "characters; those exist only in the display and will never match. "
        "To batch several edits to one file in a single call, pass `edits`: a "
        "list of items, each `{old, new, replace_all?}` with the same match "
        "rules as a single edit. The file is read once, the edits apply "
        "sequentially (each edit sees the result of the earlier ones), and the "
        "batch is all-or-nothing — if any edit fails its match rule the file is "
        "left untouched. Pass EITHER `edits` OR top-level `old`/`new`, not both."
    ),
)
async def edit_file(ctx: ToolContext, path: str, old: str | None = None,
                    new: str | None = None, *, replace_all: bool = False,
                    edits: list[dict[str, object]] | None = None) -> str:
    plan: list[tuple[str, str, bool]]
    if edits is not None:
        # Mutual exclusion: batch mode owns the whole call. Checked before any
        # FS access so a mis-shaped call never reads or writes.
        if old is not None or new is not None or replace_all:
            raise ValueError("edit_file: pass either edits (batch) or "
                             "old/new/replace_all (single), not both")
        _validate_batch(edits)
        plan = [(str(e["old"]), str(e["new"]), bool(e.get("replace_all", False))) for e in edits]
    else:
        if old is None or new is None:
            raise ValueError("edit_file requires both old and new "
                             "(or an edits batch)")
        if old == "":
            # "abc".count("") == len+1, so an empty old is never "absent" — and
            # under replace_all, body.replace("", new) would insert `new` between
            # EVERY character of the file. (Previously only the accidental
            # count("") > 1 uniqueness error blocked this, with a misleading
            # message; replace_all=True removes exactly that accident.)
            raise ValueError("old must not be empty")
        plan = [(old, new, replace_all)]
    resolved = ctx.authorize_fs(("read", "write"), path)
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
    count = 0
    for i, (o, n, ra) in enumerate(plan, 1):
        label = f"edit #{i}: " if edits is not None else ""
        body, count = _apply_edit(body, o, n, path, ra, label=label)
    new_bytes = body.encode("utf-8")
    # Bytes-faithful write-back: encode and write BYTES (no platform newline
    # translation — the old read_text() path mangled CRLF files).
    _atomic_write_bytes(resolved, new_bytes, display_path=path)
    ctx.read_state[resolved] = ReadRecord(
        mtime=resolved.stat().st_mtime,
        content_sha256=hashlib.sha256(new_bytes).hexdigest(),
        full=True,
    )
    if edits is not None:
        return f"applied {len(edits)} edits to {path}"
    if replace_all:
        return f"replaced {count} occurrences in {path}"
    return f"replaced 1 occurrence in {path}"
