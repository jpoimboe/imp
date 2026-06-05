"""The (symbol, locus) matching contract (spec Grader §, Matching semantics).
ONE implementation, used by BOTH the grader (recall fallback + precision locus
membership) and the panel/ensemble union dedup — so a panel's union and the
grader agree on what "the same finding" means."""

from __future__ import annotations

from collections.abc import Iterable

from imp.evals.models import ExpectedFinding, ReviewFinding


def _split(locus: str) -> tuple[str, str | None]:
    if ":" in locus:
        f, _, line = locus.rpartition(":")
        return f, line
    return locus, None


def _file(locus: str) -> str:
    # compare by basename so base/head/grepped line-spaces and path prefixes
    # (src/imp/... vs imp/...) don't defeat a real file match
    f, _ = _split(locus)
    return f.rsplit("/", 1)[-1]


def match_kind(
    expected_symbol: str, expected_locus: str, found_symbol: str, found_locus: str
) -> str:
    """Return "exact" | "partial" | "none". Symbol-first, line-as-hint:
    - same symbol + same file + same line  -> exact
    - same symbol (any line)               -> partial (right bug, wrong line)
    - same file + same line (symbol absent)-> exact
    - else                                 -> none
    """
    es = (expected_symbol or "").lower()
    fs = (found_symbol or "").lower()
    ef, el = _file(expected_locus), _split(expected_locus)[1]
    ff, fl = _file(found_locus), _split(found_locus)[1]

    same_symbol = bool(es) and es == fs
    same_line = ef == ff and el is not None and el == fl

    if same_symbol and same_line:
        return "exact"
    if same_symbol:
        return "partial"
    if same_line:
        return "exact"
    # NOTE: deliberately NO "same file, missing symbol -> partial" branch. That
    # over-credits: one vague file-level flag would partial-catch EVERY expected
    # finding in that file, inflating recall and leaking free true-positives into
    # precision. A catch needs a symbol OR a line match.
    return "none"


def flags_known_locus(
    finding: ReviewFinding, expected_findings: Iterable[ExpectedFinding]
) -> bool:
    """True if `finding` matches ANY labeled bug (so it is NOT a false positive)."""
    return any(
        match_kind(ef.symbol, ef.locus, finding.symbol, finding.locus) != "none"
        for ef in expected_findings
    )


DEDUP_LINE_TOLERANCE = 8  # same-symbol findings >this many lines apart are treated
#                           as DISTINCT bugs (e.g. two issues in one big function),
#                           not duplicates — dedup must not deflate panel recall.


def _is_duplicate(a: ReviewFinding, b: ReviewFinding) -> bool:
    """STRICTER than match_kind (which is lenient for recall): two flags are the
    SAME finding only if same file AND (identical line, OR same symbol within a
    small line window). This avoids collapsing same-symbol-different-bug."""
    fa, la = _file(a.locus), _split(a.locus)[1]
    fb, lb = _file(b.locus), _split(b.locus)[1]
    if fa != fb:
        return False
    if la is not None and lb is not None and la == lb:
        return True
    sa, sb = a.symbol.lower(), b.symbol.lower()
    if sa and sa == sb:
        if la is None or lb is None:
            return True  # no usable line numbers — assume the same
        try:
            return abs(int(la) - int(lb)) <= DEDUP_LINE_TOLERANCE
        except ValueError:
            return True
    return False


def dedup_findings(findings: Iterable[ReviewFinding]) -> tuple[ReviewFinding, ...]:
    """Collapse genuine duplicate flags (see _is_duplicate). Keeps the
    highest-severity representative so a panel doesn't lose a blocking flag to a
    nit duplicate."""
    order = ["Critical", "Major", "Important", "Minor", "Nit"]

    def rank(f: ReviewFinding) -> int:
        return order.index(f.severity) if f.severity in order else len(order)

    kept: list[ReviewFinding] = []
    for f in findings:
        dup_idx = next(
            (i for i, k in enumerate(kept) if _is_duplicate(k, f)),
            None,
        )
        if dup_idx is None:
            kept.append(f)
        elif rank(f) < rank(kept[dup_idx]):
            kept[dup_idx] = f
    return tuple(kept)
