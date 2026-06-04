"""One-shot: build the three curated seed fixtures from real imp history.
Run from the imp repo root: `python scripts/build_curated_fixtures.py`.
Re-runnable (overwrites the fixture dirs)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.evals.fixtures import save_fixture
from imp.evals.models import ExpectedFinding, Fixture, FixtureMeta

OUT = Path("src/imp/evals/data/curated")

# -- SHAs located in Task 8 Step 1 (verified 2026-06-03; three DISTINCT commits) --
HINT_CAP_BASE = "41e667f579c799a1cd91e862955ed0969e7b6a83"  # 00f3fc9~1
HINT_CAP_HEAD = "00f3fc9b1d5c179588ce1e99721b9f5005e8b3bd"  # adds decide() + hint-cap (in-diff)
OVERFLOW_BASE = "878068ef728f8bf9e73177e21ff83b58ec8e65ed"  # 453f746~1
OVERFLOW_HEAD = "453f7466eb48128548129b9426468db651eaee56"  # adds 1.0*(2**n) (in-diff, deep-tail)
ANYIO_BASE = "896c64792255f25b5186e92481f8fd069ca7b6ae"     # 3804c80~1 (verify per Step 1)
ANYIO_HEAD = "3804c801992d0ae9a8aab05790921c69e8d662af"     # cross-cutting anyio.sleep gap


def _diff(base: str, head: str) -> str:
    return subprocess.run(
        ["git", "diff", f"{base}..{head}"], capture_output=True, text=True, check=True
    ).stdout


def build() -> None:
    hint = Fixture(
        id="hint-cap-inversion",
        source="curated",
        base_sha=HINT_CAP_BASE,
        head_sha=HINT_CAP_HEAD,
        diff=_diff(HINT_CAP_BASE, HINT_CAP_HEAD),
        task_context=(
            "Implement per-category retry backoff in error_policy.decide(): "
            "RetryWithBackoff categories use delay = 1*2^n and honor a server "
            "Retry-After hint."
        ),
        expected_findings=(
            ExpectedFinding(
                # loci are HEAD-TREE (00f3fc9) line hints; at that SHA the hint-cap
                # line is :79 (NOT :76, which is the current tree). Matching is
                # symbol-first, but keep the label honest to its pinned tree.
                locus="src/imp/engine/error_policy.py:79",
                symbol="decide",
                description=(
                    "hint-cap inversion: min(60, max(delay, hint)) caps our own "
                    "exponential backoff, contradicting the docstring (the 60s cap "
                    "is meant for the hint only). Fires only when "
                    "hint_delay_seconds is not None."
                ),
                severity="Major",
                must_catch=True,
            ),
        ),
        # At 00f3fc9 the overflow line does not exist yet, so this fixture is clean
        # of co-located bugs. Precision anchors come from Task 26 clean fixtures, so
        # no clean_loci guess here (the earlier :69 guess was wrong for this tree).
        clean_loci=(),
        meta=FixtureMeta(
            "code-reviewer", None, True, False,
            stratum="in-diff", independence="experiment-seed",
        ),
    )

    overflow = Fixture(
        id="backoff-overflow",
        source="curated",
        base_sha=OVERFLOW_BASE,
        head_sha=OVERFLOW_HEAD,
        diff=_diff(OVERFLOW_BASE, OVERFLOW_HEAD),
        task_context=hint.task_context,
        expected_findings=(
            ExpectedFinding(
                locus="src/imp/engine/error_policy.py:74",
                symbol="decide",
                description=(
                    "deep-tail overflow at ~1024 retries: 2**1024 is a fine "
                    "arbitrary-precision int, but the float multiply 1.0 * (2**1024) "
                    "raises OverflowError('int too large to convert to float'). No "
                    "exponent cap."
                ),
                severity="Major",
                must_catch=True,
            ),
            # Co-located: the hint-cap bug also lives in this tree (it predates the
            # overflow line). Label it must_catch=False so a CORRECT hint-cap flag
            # is a true positive, not a false positive — but it isn't this
            # fixture's deep-tail target, so it doesn't drive the verdict.
            ExpectedFinding(
                locus="src/imp/engine/error_policy.py:76",
                symbol="decide",
                description=(
                    "co-located hint-cap inversion (min(60, max(delay, hint)) caps "
                    "our own exponential); present here as context, not the target."
                ),
                severity="Major",
                must_catch=False,
            ),
        ),
        clean_loci=(),
        meta=FixtureMeta(
            "code-reviewer", None, True, False,
            stratum="deep-tail", independence="experiment-seed",
        ),
    )

    anyio = Fixture(
        id="anyio-sleep-uncapped",
        source="curated",
        base_sha=ANYIO_BASE,
        head_sha=ANYIO_HEAD,
        diff=_diff(ANYIO_BASE, ANYIO_HEAD),
        task_context=(
            "Remove the per-call backoff cap so retry delay is governed centrally "
            "by error_policy/the executor wall-clock ceiling."
        ),
        expected_findings=(
            ExpectedFinding(
                locus="src/imp/agents/subagents.py:203",  # anyio.sleep at 3804c80 is line 203
                # the enclosing fn (NOT _route_agent, which is in executor.py)
                symbol="dispatch_task",
                description=(
                    "cross-cutting gap: removing the cap left a SECOND, "
                    "un-enumerated caller's anyio.sleep unbounded. A monolithic "
                    "in-diff review misses the parallel call site; a completeness "
                    "pass that greps every caller of the changed path catches it."
                ),
                severity="Major",
                must_catch=True,
            ),
        ),
        clean_loci=(),
        meta=FixtureMeta(
            "code-reviewer", None, True, True,  # was_fixed=True
            stratum="cross-cutting", independence="experiment-seed",
        ),
    )

    for fx in (hint, overflow, anyio):
        save_fixture(fx, OUT)
        print(f"wrote {OUT / fx.id}")


if __name__ == "__main__":
    build()
