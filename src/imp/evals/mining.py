"""Mine the Claude Code session logs that built imp into mined fixtures (spec
Tier 1). Discovery (this module's first half) finds review-dispatch episodes;
diff recovery (Task 18), the rework signal + dedup (Task 19), and assembly +
failure logging (Task 20) build on RawEpisode. Mining MAY yield thin and never
gates the scorecard."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# A dispatch description like "Spec review ...", "Quality review ...",
# "Code review ..." (spec mining step 1). subagent_type is the stronger signal
# when present.
_REVIEW_DESC = re.compile(r"\b(spec|quality|code)\s+review\b", re.I)


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
