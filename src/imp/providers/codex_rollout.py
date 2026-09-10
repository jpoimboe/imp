# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Persist and restore codex's native on-disk rollout across an imp pause.

codex is stateful via a rollout file at
`$CODEX_HOME/sessions/…/rollout-<ts>-<thread_id>.jsonl`. imp provisions a fresh
per-run CODEX_HOME and `shutil.rmtree`s it at teardown, so the rollout — the
actual conversation context — is deleted at the pause. To resume, imp must own
that file across the pause: copy it (and the thread_id) into the durable
`transcript_dir` on pause, restore it into the fresh CODEX_HOME on resume, then
call `Codex.resume_thread(thread_id)`.

The same file is also the only place codex reports live context occupancy and
its rate-limit level, so `latest_readings` reads both in place — no stdout JSON
mode emits them — and the only place codex records that it compacted history
(`compaction_seen`).
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imp.providers.quota import QuotaReading, QuotaWindow

_THREAD_JSON = "codex_thread.json"
_ROLLOUT_SUBDIR = "codex_rollout"


def _newest_matching_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """The rollout file for `thread_id`. codex names it
    `rollout-<ts>-<thread_id>.jsonl`; prefer the newest name-match (a chained
    resume may write a fresh rollout for the same thread), else the sole rollout.
    """
    sessions = codex_home / "sessions"
    if not sessions.exists():
        return None
    rollouts = list(sessions.rglob("*.jsonl"))
    matches = [p for p in rollouts if thread_id in p.name]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    if len(rollouts) == 1:
        return rollouts[0]
    return None


def _window_label(minutes: int) -> str:
    """From duration, never position: 300 -> `5h`, 10080 -> `7d`."""
    return f"{minutes // 60}h" if minutes < 1440 else f"{minutes // 1440}d"


def quota_from_rate_limits(block: Any) -> QuotaReading | None:
    """codex's `rate_limits` block from a `token_count` record, normalized.
    None when it carries no usable window — the caller then records nothing."""
    if not isinstance(block, dict):
        return None
    windows: list[tuple[int, QuotaWindow]] = []
    for key in ("primary", "secondary"):
        w = block.get(key)
        if not isinstance(w, dict):
            continue
        minutes = w.get("window_minutes")
        used = w.get("used_percent")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
            continue
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        resets = w.get("resets_at")
        windows.append((minutes, QuotaWindow(
            label=_window_label(minutes), used_pct=float(used),
            resets_at=(resets if isinstance(resets, int)
                       and not isinstance(resets, bool) else None))))
    if not windows:
        return None
    plan = block.get("plan_type")
    return QuotaReading(
        provider="codex", plan=plan if isinstance(plan, str) else None,
        windows=tuple(w for _, w in sorted(windows, key=lambda pair: pair[0])),
        raw=block)


@dataclass(frozen=True, slots=True)
class RolloutReadings:
    """Both readings one rollout pass yields. Each is the last VALID of its own
    kind: `rate_limits` can be null on a record whose token counts are good."""

    context: tuple[int, int] | None
    quota: QuotaReading | None


@dataclass(frozen=True, slots=True)
class RolloutUsage:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @classmethod
    def from_mapping(cls, value: Any) -> RolloutUsage | None:
        if not isinstance(value, dict):
            return None
        names = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        if any(
            not isinstance(value.get(name), int) or isinstance(value.get(name), bool)
            for name in names
        ):
            return None
        return cls(**{name: value[name] for name in names})


_FileIdentity = tuple[int, int]
_CursorEntry = tuple[Path, _FileIdentity, int]


@dataclass(frozen=True, slots=True)
class RolloutCursor:
    entries: tuple[_CursorEntry, ...]
    discovery_complete: bool

    def as_dict(self) -> dict[_FileIdentity, int]:
        return {identity: frontier for _, identity, frontier in self.entries}


@dataclass(frozen=True, slots=True)
class CorrelatedReadings:
    matched: bool
    readings: RolloutReadings


@dataclass(frozen=True, slots=True)
class _TokenCountRecord:
    identity: _FileIdentity
    start: int
    usage: RolloutUsage
    readings: RolloutReadings


@dataclass(slots=True)
class _RolloutGeneration:
    path: Path
    identity: _FileIdentity
    frontier: int = 0


@dataclass(frozen=True, slots=True)
class _RolloutDiscovery:
    candidates: tuple[tuple[Path, _FileIdentity], ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class _DiscoveredCandidate:
    path: Path
    identity: _FileIdentity
    mtime_ns: int


_EMPTY_READINGS = RolloutReadings(context=None, quota=None)


def _identity(stat_result: os.stat_result) -> _FileIdentity:
    return (stat_result.st_dev, stat_result.st_ino)


def _discover_rollouts(codex_home: Path, thread_id: str | None) -> _RolloutDiscovery:
    sessions = codex_home / "sessions"
    try:
        sessions.stat()
    except FileNotFoundError:
        return _RolloutDiscovery(candidates=(), complete=True)
    except OSError:
        return _RolloutDiscovery(candidates=(), complete=False)

    complete = True
    found: list[_DiscoveredCandidate] = []
    try:
        paths = sessions.rglob("*.jsonl")
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
                stat_result = resolved.stat()
            except (OSError, RuntimeError):
                complete = False
                continue
            found.append(_DiscoveredCandidate(
                path=resolved,
                identity=_identity(stat_result),
                mtime_ns=stat_result.st_mtime_ns,
            ))
    except OSError:
        complete = False

    if thread_id is None:
        selected = found
    else:
        matches = [candidate for candidate in found if thread_id in candidate.path.name]
        selected = matches if matches else found if len(found) == 1 else []
    selected.sort(key=lambda candidate: (candidate.mtime_ns, str(candidate.path)))
    return _RolloutDiscovery(
        candidates=tuple((candidate.path, candidate.identity) for candidate in selected),
        complete=complete,
    )


def _context_from_info(info: Any) -> tuple[int, int] | None:
    "`(last_token_usage.total_tokens, model_context_window)`, never partial."
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    window = info.get("model_context_window")
    if (isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0
            and isinstance(window, int) and not isinstance(window, bool) and window > 0):
        return (tokens, window)
    return None


def _token_count_record(
    line: bytes, identity: _FileIdentity, start: int,
) -> _TokenCountRecord | None:
    if b"token_count" not in line:
        return None
    try:
        record = json.loads(line)
        payload = record["payload"]
        if record["type"] != "event_msg" or payload["type"] != "token_count":
            return None
        info = payload["info"]
        if not isinstance(info, dict):
            return None
        usage = RolloutUsage.from_mapping(info.get("total_token_usage"))
        if usage is None:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return _TokenCountRecord(
        identity=identity,
        start=start,
        usage=usage,
        readings=RolloutReadings(
            context=_context_from_info(info),
            quota=quota_from_rate_limits(payload.get("rate_limits")),
        ),
    )


@dataclass(slots=True)
class RolloutTelemetry:
    codex_home: Path
    thread_id: str | None
    _generations: list[_RolloutGeneration] = field(default_factory=list, init=False)
    _pending: list[RolloutUsage] = field(default_factory=list, init=False)

    def _discover(self) -> _RolloutDiscovery:
        discovery = _discover_rollouts(self.codex_home, self.thread_id)
        known = {generation.identity: generation for generation in self._generations}
        for path, identity in discovery.candidates:
            generation = known.get(identity)
            if generation is None:
                generation = _RolloutGeneration(path=path, identity=identity)
                self._generations.append(generation)
                known[identity] = generation
            else:
                generation.path = path
        return discovery

    def _records(self, generation: _RolloutGeneration) -> Iterator[_TokenCountRecord]:
        try:
            with generation.path.open("rb") as stream:
                if _identity(os.fstat(stream.fileno())) != generation.identity:
                    return
                stream.seek(generation.frontier)
                while True:
                    start = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        return
                    generation.frontier = stream.tell()
                    record = _token_count_record(line, generation.identity, start)
                    if record is not None:
                        yield record
        except OSError:
            return

    def _consume_pending(self, usage: RolloutUsage) -> bool:
        try:
            index = self._pending.index(usage)
        except ValueError:
            return False
        self._pending.pop(index)
        return True

    def begin_turn(self) -> RolloutCursor:
        discovery = self._discover()
        discovered = {identity for _, identity in discovery.candidates}
        for generation in self._generations:
            if generation.identity not in discovered:
                continue
            for record in self._records(generation):
                self._consume_pending(record.usage)
        return RolloutCursor(
            entries=tuple(
                (generation.path, generation.identity, generation.frontier)
                for generation in self._generations
                if generation.identity in discovered
            ),
            discovery_complete=discovery.complete,
        )

    def scan_current(
        self, cursor: RolloutCursor, usage: RolloutUsage,
    ) -> CorrelatedReadings:
        discovery = self._discover()
        discovered = {identity for _, identity in discovery.candidates}
        cursor_frontiers = cursor.as_dict()
        matched = False
        readings: RolloutReadings | None = None
        for generation in self._generations:
            if (
                generation.identity not in discovered
                or (
                    not cursor.discovery_complete
                    and generation.identity not in cursor_frontiers
                )
            ):
                continue
            for record in self._records(generation):
                if self._consume_pending(record.usage):
                    continue
                cursor_frontier = cursor_frontiers.get(record.identity)
                eligible = (
                    record.start >= cursor_frontier
                    if cursor_frontier is not None
                    else cursor.discovery_complete
                )
                if not eligible or record.usage != usage:
                    continue
                matched = True
                if readings is None and record.readings.context is not None:
                    readings = record.readings
        return CorrelatedReadings(
            matched=matched,
            readings=readings if readings is not None else _EMPTY_READINGS,
        )

    def finish_turn(
        self, cursor: RolloutCursor, usage: RolloutUsage, matched: bool,
    ) -> None:
        if cursor.discovery_complete and not matched:
            self._pending.append(usage)


def latest_readings(codex_home: Path, thread_id: str) -> RolloutReadings:
    """codex's own live context occupancy and rate-limit level, from one pass
    over the rollout — the only place either is reported. Both halves are
    absent when the rollout is missing or carries no usable `token_count`."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return RolloutReadings(context=None, quota=None)
    context: tuple[int, int] | None = None
    quota: QuotaReading | None = None
    with rollout.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            # Pre-filter: rollouts run to megabytes, few lines are token_count.
            if "token_count" not in line:
                continue
            try:
                rec = json.loads(line)
                payload = rec["payload"]
                if rec["type"] != "event_msg" or payload["type"] != "token_count":
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            reading = _context_from_info(payload.get("info"))
            if reading is not None:
                context = reading
            block = quota_from_rate_limits(payload.get("rate_limits"))
            if block is not None:
                quota = block
    return RolloutReadings(context=context, quota=quota)


def compaction_seen(codex_home: Path, thread_id: str) -> bool:
    """True when codex compacted its own history in this rollout — a top-level
    `{"type": "compacted"}` record. Nothing about a compaction reaches the
    `codex exec --json` stdout stream, so the rollout is the only signal."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return False
    with rollout.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            # Pre-filter: rollouts run to megabytes, few lines mention it.
            if "compacted" not in line:
                continue
            try:
                if json.loads(line)["type"] == "compacted":
                    return True
            except (ValueError, KeyError, TypeError):
                continue
    return False


def persist_rollout(*, codex_home: Path, thread_id: str, transcript_dir: Path) -> bool:
    """Copy codex's rollout out of the ephemeral CODEX_HOME into `transcript_dir`
    and record `thread_id` + the rollout's relative path. Call BEFORE
    `teardown_codex_home`. Returns True iff a rollout was persisted (False when
    codex wrote none yet — e.g. a run that failed before its first turn)."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return False
    rel = rollout.relative_to(codex_home)  # sessions/…/rollout-….jsonl
    dest = transcript_dir / _ROLLOUT_SUBDIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rollout, dest)
    (transcript_dir / _THREAD_JSON).write_text(
        json.dumps({"thread_id": thread_id, "rollout_relpath": str(rel)}))
    return True


def restore_rollout(
    *, codex_home: Path, transcript_dir: Path,
) -> str:
    """Restore the persisted rollout into the fresh CODEX_HOME and return the
    saved thread_id. Call AFTER provisioning the fresh CODEX_HOME and BEFORE
    `Codex.resume_thread`. Fails loudly (`FileNotFoundError`) if the thread
    metadata or rollout bytes are missing — converting otherwise-silent amnesia
    (teardown ran too early, ephemeral CI, cross-host) into a real error.
    """
    meta_path = transcript_dir / _THREAD_JSON
    if not meta_path.exists():
        raise FileNotFoundError(
            f"codex resume: no {_THREAD_JSON} in {transcript_dir} — "
            "rollout was not persisted on pause")
    meta = json.loads(meta_path.read_text())
    thread_id: str = meta["thread_id"]
    rel: str = meta["rollout_relpath"]
    saved = transcript_dir / _ROLLOUT_SUBDIR / rel
    if not saved.exists():
        raise FileNotFoundError(
            f"codex resume: persisted rollout missing at {saved}")
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(saved, dest)
    if not dest.exists():  # the spec's explicit "assert it exists" guard
        raise FileNotFoundError(
            f"codex resume: rollout restore failed at {dest}")
    return thread_id
