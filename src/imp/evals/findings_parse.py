"""Best-effort parse of a reviewer's `findings` prose into discrete
ReviewFindings (locus + severity + blocking + symbol). Used for PRECISION (what
loci did the review flag) and panel dedup. The semantic RECALL match is the LLM
judge's job (grader.py) — this parser is intentionally simple and deterministic."""

from __future__ import annotations

import re

from imp.evals.models import BLOCKING_SEVERITIES, ReviewFinding

# Maps a section header to its canonical severity. We scan section by section so
# each bullet inherits the severity of the heading above it.
_SECTION_SEVERITY = [
    (re.compile(r"#+\s*critical", re.I), "Critical"),
    (re.compile(r"#+\s*(major)", re.I), "Major"),
    (re.compile(r"#+\s*important", re.I), "Important"),
    (re.compile(r"#+\s*minor\s*/\s*nit", re.I), "Nit"),
    (re.compile(r"#+\s*minor", re.I), "Minor"),
    (re.compile(r"#+\s*nit", re.I), "Nit"),
]
# `file.py:123` or `path/to/file.py:123`
_LOCUS = re.compile(r"`?([\w./-]+\.\w+:\d+)`?")
# a `symbol()` or `symbol` token in backticks that is NOT a path:line
_SYMBOL = re.compile(r"`([A-Za-z_][\w]*)\(?\)?`")


def _severity_for(line: str, current: str) -> str:
    for pat, sev in _SECTION_SEVERITY:
        if pat.search(line):
            return sev
    return current


def _symbol_for(text: str, locus: str) -> str:
    # Prefer an explicit `symbol` token; fall back to the locus filename stem.
    for m in _SYMBOL.finditer(text):
        token = m.group(1)
        if f"{token}:" not in locus and not locus.endswith(token):
            return token
    file_part = locus.split(":", 1)[0]
    return file_part.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def parse_review_findings(prose: str) -> tuple[ReviewFinding, ...]:
    findings: list[ReviewFinding] = []
    current = "Important"  # default for a bullet under an unlabeled Issues block
    in_strengths = False
    for raw in prose.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"#+\s*strength", line, re.I):
            in_strengths = True
            continue
        if line.startswith("#"):
            in_strengths = re.search(r"strength", line, re.I) is not None
            current = _severity_for(line, current)
            continue
        if in_strengths:
            continue
        for lm in _LOCUS.finditer(line):
            locus = lm.group(1)
            sev = current
            findings.append(
                ReviewFinding(
                    symbol=_symbol_for(line, locus),
                    locus=locus,
                    description=line.lstrip("-* ").strip(),
                    severity=sev,
                    blocking=sev in BLOCKING_SEVERITIES,
                )
            )
    return tuple(findings)
