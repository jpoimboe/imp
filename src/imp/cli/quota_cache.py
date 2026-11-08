# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""A disposable cache of each quest's quota extract under `~/.imp/cache/quota/`.

An entry holds only facts read from that quest's own log; weeks, prices and
metering are applied on read, so no change to another quest or to the price
table can make it stale. A lost or damaged entry costs only a recompute."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from imp.cli.quota_share import Increment, QuotaExtract, Reading, extract
from imp.cli.store import _parse_jsonl
from imp.paths import imp_home
from imp.providers.catalog import BillingUsage

# Bump whenever extract(), anything it calls, or the entry layout changes.
EXTRACT_VERSION = 1


def cache_dir() -> Path:
    return imp_home() / "cache" / "quota"


def cached_extract(project: str, quest_id: str, key: list[str | int],
                   read: Callable[[], bytes | None]) -> QuotaExtract | None:
    """`quest_id`'s extract in `project`: its entry when `key` still matches, else
    one parsed from `read()` and written back. None when `read()` finds no log."""
    path = _entry_path(project, quest_id)
    stamp: list[str | int] = [EXTRACT_VERSION, *key]
    hit = _load(path, stamp)
    if hit is not None:
        return hit
    raw = read()
    if raw is None:
        return None
    found = extract(_parse_jsonl(raw))
    _store(path, stamp, found)
    return found


def _entry_path(project: str, quest_id: str) -> Path:
    name = hashlib.sha256(f"{project}\0{quest_id}".encode()).hexdigest()
    return cache_dir() / f"{name}.json"


def _load(path: Path, stamp: list[str | int]) -> QuotaExtract | None:
    try:
        doc = json.loads(path.read_bytes())
        if doc["key"] != stamp:
            return None
        return _decode(doc)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _decode(doc: Any) -> QuotaExtract:
    return QuotaExtract(
        tuple(Increment(
            index=i["index"], provider=i["provider"], auth_mode=i["auth_mode"],
            billing=tuple((model, BillingUsage(**b)) for model, b in i["billing"]),
            start=None if i["start"] is None else datetime.fromisoformat(i["start"]),
            end=datetime.fromisoformat(i["end"]))
            for i in doc["increments"]),
        tuple(Reading(datetime.fromisoformat(r["when"]), r["provider"],
                      r["used_pct"], r["resets_at"])
              for r in doc["readings"]))


def _encode(stamp: list[str | int], found: QuotaExtract) -> bytes:
    return json.dumps({
        "key": stamp,
        "increments": [
            {"index": i.index, "provider": i.provider, "auth_mode": i.auth_mode,
             "billing": [[model, asdict(b)] for model, b in i.billing],
             "start": None if i.start is None else i.start.isoformat(),
             "end": i.end.isoformat()}
            for i in found.increments],
        "readings": [
            {"when": r.when.isoformat(), "provider": r.provider,
             "used_pct": r.used_pct, "resets_at": r.resets_at}
            for r in found.readings],
    }, separators=(",", ":")).encode()


def _store(path: Path, stamp: list[str | int], found: QuotaExtract) -> None:
    """Publish by rename so a concurrent reader never sees half an entry. A
    failed write is dropped: the next read recomputes."""
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(_encode(stamp, found))
            os.replace(tmp, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise
