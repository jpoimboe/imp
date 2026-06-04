"""Mine the Claude Code session logs that built imp into mined fixtures (spec
Tier 1). Discovery (this module's first half) finds review-dispatch episodes;
diff recovery (Task 18), the rework signal + dedup (Task 19), and assembly +
failure logging (Task 20) build on RawEpisode. Mining MAY yield thin and never
gates the scorecard."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from imp.evals.models import ExpectedFinding, Fixture, FixtureMeta

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
    blocking: bool = False
    was_fixed: bool = False


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


# Sampled from real review proses (heterogeneous — PASS/FAIL, "Must Fix",
# ✅/❌, free "verdict" lines). Heuristic; the must_catch labels it feeds are
# spot-checked in the normalization calibration (Task 16).
# `(?<!no )blocking\b`: the bare word "blocking" is a blocking signal EXCEPT in the
# approval phrase "no blocking issues" — the negative lookbehind drops that case.
_BLOCKING_KEYWORDS = re.compile(
    r"task_gap|plan_gap|request[\s_-]*changes?|changes?\s+requested|"
    r"must[\s_-]*fix|\bblocker\b|(?<!no )blocking\b|:\s*fail\b|❌",
    re.I,
)
_SEV_HEADER = re.compile(r"^#+\s*(critical|major)\b", re.I)
# An inline `#### Critical: <real content>` on a single line (content after the
# colon) — distinct from the bare boilerplate header `#### Critical (Must Fix)`.
_INLINE_SEV = re.compile(r"#+\s*(critical|major)\s*:\s*\S", re.I)
_EMPTY_SECTION = re.compile(r"^\s*[-*]?\s*(none|n/?a|nothing|no\b.*\bissues?)\b", re.I)
_APPROVE_VOCAB = re.compile(
    r"\bapprove[d]?\b|\bLGTM\b|:\s*pass\b|✅|no\s+blocking|"
    r"no\s+(critical|major)\s+issues",
    re.I,
)


def _sev_header_has_content(prose: str) -> bool:
    """A Critical/Major section counts as blocking ONLY if it has real content —
    reviewers emit `#### Critical (Must Fix)` headers unconditionally and write
    'None' underneath when there's nothing (M9). Don't treat the bare header as
    a blocking signal."""
    lines = prose.splitlines()
    for i, line in enumerate(lines):
        if _SEV_HEADER.match(line.strip()):
            for nxt in lines[i + 1 : i + 6]:
                s = nxt.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    break  # next header -> this section was empty
                if _EMPTY_SECTION.match(s):
                    break  # "None" / "N/A" -> empty
                return True  # a real bullet under the header
    return False


def verdict_is_blocking(prose: str) -> bool:
    """Heuristic: did the review request changes / raise a NON-EMPTY blocking
    issue? Keyword signal (scanned on non-header lines, so a boilerplate
    `#### Critical (Must Fix)` header doesn't self-trigger the 'must fix'
    keyword — M9) OR a Critical/Major header with actual content OR an inline
    `#### Critical: <content>`."""
    non_header = "\n".join(
        ln for ln in prose.splitlines() if not ln.lstrip().startswith("#")
    )
    if _BLOCKING_KEYWORDS.search(non_header):
        return True
    if _sev_header_has_content(prose):
        return True
    return bool(_INLINE_SEV.search(prose))


def verdict_is_approve(prose: str) -> bool:
    """An explicit approval with no blocking signal (used by the block→fixed
    rework heuristic to spot a later clean re-review)."""
    return _APPROVE_VOCAB.search(prose) is not None and not verdict_is_blocking(prose)


def mark_rework(episodes: list[RawEpisode]) -> list[RawEpisode]:
    """Set `blocking` on every episode, and `was_fixed` on a blocking episode only
    when a LATER episode **on the SAME head_sha** approves. SAME-SHA scoping is
    load-bearing: a corpus-global "any later approval" fires on nearly every
    blocking episode in a 586-dispatch corpus (there's always *some* later
    approval somewhere) and turns the ground truth into noise. Must run BEFORE
    dedup (dedup would drop the earlier blocking pass).

    LIMITATION (documented, conservative): descendant-SHA rework (block on H1, fix
    in child H2, approve on H2) is NOT detected in v1 — same-SHA re-review after a
    fix is rare, so `was_fixed` fires seldom. That's fine: must_catch then comes
    from the normalizer's per-finding blocking signal (the parity lower bound);
    `was_fixed` is only a high-confidence BONUS, and a rare-but-correct signal
    beats a frequent-but-wrong one."""
    ordered = sorted(episodes, key=lambda e: e.timestamp or "")
    heads = [extract_shas(e.dispatch_prompt)[1] for e in ordered]
    out: list[RawEpisode] = []
    for i, ep in enumerate(ordered):
        blocking = verdict_is_blocking(ep.review_prose)
        head = heads[i]
        later_same_sha_approve = head is not None and any(
            heads[j] == head and verdict_is_approve(ordered[j].review_prose)
            for j in range(i + 1, len(ordered))
        )
        out.append(
            RawEpisode(
                session_file=ep.session_file,
                tool_use_id=ep.tool_use_id,
                reviewer_type=ep.reviewer_type,
                dispatch_prompt=ep.dispatch_prompt,
                review_prose=ep.review_prose,
                timestamp=ep.timestamp,
                blocking=blocking,
                was_fixed=blocking and later_same_sha_approve,
            )
        )
    return out


def dedup_episodes(episodes: list[RawEpisode]) -> list[RawEpisode]:
    """Collapse rework passes per (head_sha, reviewer). Run AFTER mark_rework.
    Keeps a BLOCKING representative when the group has one (it carries the bug
    labels, not the later approval's empty findings) and OR-s `was_fixed` across
    the group so the signal survives dedup."""
    from dataclasses import replace

    groups: dict[tuple[str | None, str], list[RawEpisode]] = {}
    for ep in episodes:
        _base, head = extract_shas(ep.dispatch_prompt)
        groups.setdefault((head, ep.reviewer_type), []).append(ep)
    out: list[RawEpisode] = []
    for group in groups.values():
        blocking_eps = [e for e in group if e.blocking]
        rep = max(blocking_eps or group, key=lambda e: e.timestamp or "")
        out.append(replace(rep, was_fixed=any(e.was_fixed for e in group)))
    return out


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


# Strip the SAME label forms `extract_shas` accepts (BASE/HEAD with an optional
# `_SHA` suffix + `:`/`=`), so a bare `BASE:`/`HEAD:` SHA the extractor uses can't
# leak into task_context, plus any `git diff`/`git show` instruction line.
_SHA_OR_GITDIFF_LINE = re.compile(
    r"^.*(\b(?:BASE|HEAD)(?:[_ ]?SHA)?\s*[:=]|git (?:diff|show)).*$", re.I | re.M
)


def _clean_task_context(dispatch_prompt: str) -> str:
    """Strip SHA labels and `git diff`/`git show` instruction lines from a mined
    dispatch prompt so mined `task_context` doesn't LEAK the answer (SHAs, "look
    here" hints) or carry a richer/leakier context than curated fixtures' neutral
    one-paragraph task_context (asymmetry would bias a mined-vs-curated recall
    comparison). Keeps the substantive task description."""
    cleaned = _SHA_OR_GITDIFF_LINE.sub("", dispatch_prompt)
    return "\n".join(ln for ln in cleaned.splitlines() if ln.strip()).strip()


@dataclass(frozen=True)
class MiningDrop:
    tool_use_id: str
    session_file: str
    reason: str  # "no_shas" | "tree_unreconstructable"


@dataclass(frozen=True)
class MiningOutcome:
    fixtures: list[Fixture]
    drops: list[MiningDrop]


def assemble_fixtures(
    episodes: list[RawEpisode],
    *,
    control_root: Path,
    normalize: Callable[[str], tuple[ExpectedFinding, ...]],
) -> MiningOutcome:
    """Build mined Fixtures from episodes by reconstructing each diff via
    `git diff BASE..HEAD` against `control_root` (the verified mechanism — the
    diff is not in the log). `normalize` turns prose into ExpectedFindings (inject
    normalize.normalize_findings in production; a fake in tests). Episodes missing
    SHAs (no_shas) or whose SHAs no longer resolve (tree_unreconstructable) are
    dropped and logged — never silently skipped (spec Risks §)."""
    fixtures: list[Fixture] = []
    drops: list[MiningDrop] = []
    for ep in episodes:
        base, head = extract_shas(ep.dispatch_prompt)
        if not base or not head:
            drops.append(MiningDrop(ep.tool_use_id, ep.session_file, "no_shas"))
            continue
        diff = reconstruct_diff(control_root, base, head)
        if not diff:
            drops.append(MiningDrop(ep.tool_use_id, ep.session_file, "tree_unreconstructable"))
            continue
        findings = normalize(ep.review_prose)
        # blocking→fixed promotes findings to must_catch (high-confidence real bug);
        # otherwise keep the normalizer's per-finding blocking call.
        if ep.was_fixed:
            findings = tuple(
                ExpectedFinding(f.locus, f.symbol, f.description, f.severity, True)
                for f in findings
            )
        fixtures.append(
            Fixture(
                id=f"mined-{ep.reviewer_type}-{head[:8]}",
                source="mined",
                base_sha=base,
                head_sha=head,
                diff=diff,
                task_context=_clean_task_context(ep.dispatch_prompt),
                expected_findings=findings,
                clean_loci=(),  # mined merged-clean is directional only (grader handles it)
                meta=FixtureMeta(
                    reviewer_type=ep.reviewer_type,
                    original_model=None,
                    blocking=ep.blocking,
                    was_fixed=ep.was_fixed,
                ),
            )
        )
    return MiningOutcome(fixtures=fixtures, drops=drops)
