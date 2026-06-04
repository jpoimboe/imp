"""Mine the Claude Code session logs that built imp into mined fixtures (spec
Tier 1). Discovery (this module's first half) finds review-dispatch episodes;
diff recovery (Task 18), the rework signal + dedup (Task 19), and assembly +
failure logging (Task 20) build on RawEpisode. Mining MAY yield thin and never
gates the scorecard."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# A dispatch description like "Spec review ...", "Quality review ...",
# "Code review ..." (spec mining step 1). subagent_type is the stronger signal
# when present.
_REVIEW_DESC = re.compile(r"\b(spec|quality|code)\s+review\b", re.I)

# add near the other regexes (BOTH cases, optional `_SHA`/` SHA` suffix, colon
# or equals separator). The SHA suffix is OPTIONAL so a bare `base:`/`head:` label
# also matches. Only HEAD/BASE — the "Commit SHA" alt was a foot-gun from the
# de-scoped bash-result path and matches truncated 8-hex SHAs in review prose.
# Leading \b anchors keep a bare `base:`/`head:` from matching inside `database:`
# / `ahead:` followed by a hex value (the optional-SHA suffix widened that risk).
_HEAD_SHA = re.compile(r"\bHEAD(?:[_ ]?SHA)?\s*[:=]\s*`?([0-9a-f]{7,40})", re.I)
_BASE_SHA = re.compile(r"\bBASE(?:[_ ]?SHA)?\s*[:=]\s*`?([0-9a-f]{7,40})", re.I)


@dataclass(frozen=True)
class RawEpisode:
    session_file: str
    tool_use_id: str
    reviewer_type: str
    dispatch_prompt: str
    review_prose: str
    timestamp: str | None


def _records(path: Path) -> Iterator[dict[str, object]]:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _content_blocks(record: dict[str, object]) -> list[dict[str, object]]:
    msg = record.get("message") or {}
    blocks = (msg if isinstance(msg, dict) else {}).get("content")
    return blocks if isinstance(blocks, list) else []


def _result_text(block: dict[str, object]) -> str:
    """tool_result content is either a list of {type:text,text} or a bare str."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def _reviewer_type(tool_input: dict[str, object]) -> str | None:
    """Derive the reviewer type from the dispatch DESCRIPTION. (Verified: real
    logs set subagent_type to general-purpose/Explore, never a reviewer name —
    the description is the only signal.)"""
    desc = tool_input.get("description", "") or ""
    m = _REVIEW_DESC.search(str(desc))
    if not m:
        return None
    return {
        "spec": "spec-task-reviewer",
        "quality": "quality-reviewer",
        "code": "code-reviewer",
    }[m.group(1).lower()]


def discover_episodes(session_file: Path) -> list[RawEpisode]:
    """Find review-dispatch Task tool_uses and pair each with its tool_result
    prose (matched by tool_use_id across the file)."""
    dispatches: dict[str, tuple[str, str, str | None]] = {}  # id -> (rtype, prompt, ts)
    results: dict[str, str] = {}
    for rec in _records(session_file):
        ts = rec.get("timestamp")
        for blk in _content_blocks(rec):
            # verified: dispatch tool is Agent, never Task
            if blk.get("type") == "tool_use" and blk.get("name") == "Agent":
                raw_input = blk.get("input") or {}
                tool_input = raw_input if isinstance(raw_input, dict) else {}
                rtype = _reviewer_type(tool_input)
                if rtype is not None:
                    prompt = tool_input.get("prompt", "") or ""
                    dispatches[str(blk.get("id", ""))] = (
                        rtype,
                        str(prompt),
                        str(ts) if ts is not None else None,
                    )
            elif blk.get("type") == "tool_result":
                results[str(blk.get("tool_use_id", ""))] = _result_text(blk)

    episodes: list[RawEpisode] = []
    for tid, (rtype, prompt, ts) in dispatches.items():
        prose = results.get(tid, "")
        if not prose:
            continue  # no returned review (Task 20 logs these as drops)
        episodes.append(
            RawEpisode(
                session_file=str(session_file),
                tool_use_id=tid,
                reviewer_type=rtype,
                dispatch_prompt=prompt,
                review_prose=prose,
                timestamp=ts,
            )
        )
    return episodes


def extract_shas(dispatch_prompt: str) -> tuple[str | None, str | None]:
    """Return (base_sha, head_sha) from the prompt's BASE_SHA:/HEAD_SHA: labels.
    Either may be None."""
    base_m = _BASE_SHA.search(dispatch_prompt)
    head_m = _HEAD_SHA.search(dispatch_prompt)
    return (
        base_m.group(1) if base_m else None,
        head_m.group(1) if head_m else None,
    )


def reconstruct_diff(control_root: Path, base_sha: str, head_sha: str) -> str:
    """`git diff base..head` against the live repo — the verified recovery
    mechanism (the diff is NOT in the log). Returns '' if either SHA does not
    resolve (rebased/orphaned history; caller drops + logs as 'tree_unrecon').

    Two-dot `A..B` here is a TREE DELTA (identical to `git diff A B` — verified;
    `..` only means a commit RANGE for `git log`, not `git diff`). So a
    multi-commit task reconstructs the full cumulative base->head change and
    applies cleanly onto a base worktree — no `head~1..head` needed.
    `errors='replace'` keeps a binary-file diff from killing the whole mine run
    with UnicodeDecodeError; such a diff may then fail `git apply` and be logged
    as tree_unreconstructable, which is correct."""
    for sha in (base_sha, head_sha):
        if subprocess.run(
            ["git", "cat-file", "-e", sha], cwd=control_root, capture_output=True
        ).returncode != 0:
            return ""
    proc = subprocess.run(
        ["git", "diff", f"{base_sha}..{head_sha}"],
        cwd=control_root,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else ""
