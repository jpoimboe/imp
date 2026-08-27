# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Durable record of a transient read fault: a JSONL log plus the bad bytes.

Forensics:
btrfs+zstd can transiently serve a mis-decompressed cached copy of a file that is
valid on disk — the observed signature is the content minus its first byte
(head-drop). Every writer of the affected files is atomic (tmp -> fsync ->
os.replace) and is the only writer, so a parse failure on a file that is valid on
disk is a read-path fault, not real corruption: dropping the file's cached pages
(pagecache.drop_page_cache) forces the next read to re-fetch the extent from disk,
which recovers. The one occurrence actually observed was an `imp resume` ->
infer_session_result -> State.load, outside any run and so with no EventLog in
existence — which is why the file here is the primary sensor and the runner_warning
only the discoverability layer. The workaround shipped in 3ba8752b0 (2026-06-28); a
later 169M-read sweep across both state locations, using a detector proven against
injected head-drops, found zero occurrences — but it ran several kernel moves after
the one the fault was observed on, so the hazard is plausibly dormant rather than
fixed. This module is what tells us if it returns. Evidence lands in
imp_home()/diagnostics, machine-global and outside every quest home, so it outlives
the quest that recorded it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from imp.paths import display_path, imp_home

if TYPE_CHECKING:
    from imp.engine.state import EventLog

Site = Literal["state", "archive_record", "event_tail"]

# Re-reads attempted at a recovery site before the bytes are declared genuinely bad.
READ_RETRIES = 4

_SEQ = itertools.count(1)  # two faults in the same second must not collide

_sink: EventLog | None = None  # one run per process; see executor's os.chdir NOTE


@contextmanager
def event_sink(log: EventLog) -> Iterator[None]:
    """Emit a runner_warning for every record() made inside this block."""
    global _sink
    _sink = log
    try:
        yield
    finally:
        _sink = None


def _preserve(diag: Path, name: str, bad: bytes) -> Path | None:
    blob = diag / "bytes" / name
    try:
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(bad)
    except OSError:
        return None
    return blob


def _append_line(diag: Path, line: dict[str, Any]) -> None:
    try:
        diag.mkdir(parents=True, exist_ok=True)
        with (diag / "read-faults.jsonl").open("a") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError:
        pass


def _subject(site: Site, path: Path) -> str | None:
    if site == "state":
        return "state.json"
    if site == "archive_record":
        return f"archive record {path.name}"
    return None  # event_tail is the CLI process: no log, no warning


def _shape(bad: bytes, good: bytes) -> str:
    if bad == good[1:]:
        return "head-drop"
    if bad == good[:-1]:
        return "tail-drop"
    return "other shape"


def _emit_warning(
    site: Site, path: Path, *, attempt: int | None, bad: bytes, good: bytes | None, diag: Path
) -> None:
    log, subject = _sink, _subject(site, path)
    if log is None or subject is None:
        return
    # whole body suppressed: display_path's Path.home() raises on unresolvable HOME
    with suppress(Exception):
        evidence = display_path(diag / "read-faults.jsonl")
        if good is None:
            message = (
                f"read fault NOT recovered — {subject} still unparseable "
                f"after {READ_RETRIES} re-reads"
            )
            detail = f"{path} — bad {len(bad)}B; evidence: {evidence}"
        else:
            message = (
                f"transient read fault recovered — {subject} re-read cleanly "
                f"on attempt {attempt} ({_shape(bad, good)})"
            )
            detail = f"{path} — bad {len(bad)}B, good {len(good)}B; evidence: {evidence}"
        log.append(
            agent_id=None, type="runner_warning", payload={"message": message, "detail": detail}
        )


def record(
    site: Site,
    path: Path,
    *,
    attempt: int | None,
    reason: str,
    bad: bytes,
    good: bytes | None,
) -> Path | None:
    """Append one line to imp_home()/diagnostics/read-faults.jsonl and preserve
    `bad` as a blob beside it; return the blob path, or None if it could not be
    written. Best-effort throughout: never raises."""
    now = datetime.now(UTC)
    diag = imp_home() / "diagnostics"
    blob = _preserve(
        diag, f"{now.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{site}-{next(_SEQ)}.bad", bad
    )
    line: dict[str, Any] = {
        "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),
        "site": site,
        "path": str(path),
        "recovered": good is not None,
        "attempt": attempt,
        "reason": reason,
        "bad_len": len(bad),
        "good_len": None if good is None else len(good),
        "head_drop": None if good is None else bad == good[1:],
        "tail_drop": None if good is None else bad == good[:-1],
        "bad_sha1": hashlib.sha1(bad).hexdigest(),
        "good_sha1": None if good is None else hashlib.sha1(good).hexdigest(),
        "bytes": None if blob is None else f"bytes/{blob.name}",
    }
    _append_line(diag, line)
    _emit_warning(site, path, attempt=attempt, bad=bad, good=good, diag=diag)
    return blob
