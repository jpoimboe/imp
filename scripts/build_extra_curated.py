"""Build the extra curated fixtures beyond the three experiment seeds: one
INDEPENDENT cross-cutting fixture (sourced from real imp history, unrelated to the
anyio.sleep experiment) + three CLEAN precision anchors (small diffs verified
bug-free by hand). Run from the imp repo root: `python scripts/build_extra_curated.py`.
Re-runnable (overwrites the fixture dirs)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.evals.fixtures import save_fixture
from imp.evals.models import ExpectedFinding, Fixture, FixtureMeta

OUT = Path("src/imp/evals/data/curated")


def _diff(base: str, head: str) -> str:
    return subprocess.run(
        ["git", "diff", f"{base}..{head}"], capture_output=True, text=True, check=True
    ).stdout


# -- Independent cross-cutting fixture (VERIFIED 2026-06-04) --
# head bb5e2a4 added `{**os.environ, **engine_env(...)}` to the bash tool; the
# SIBLING subprocess-spawn site run_script_gate (gates.py:53, `os.environ.copy()`,
# added earlier in 442d589) has the identical full-ambient-environ merge. Both leak
# an IMP_LINT_COMMAND inherited from an OUTER imp process, falsely enabling the lint
# gate. Fixed later in de8a1fa via overlay_engine_env (strips inherited engine vars
# in BOTH sites) -> provably a real bug. Independent of the anyio.sleep experiment.
CROSS_ENV_BASE = "77cedcd753b4200f21a14be419eff44cbb5bb156"  # bb5e2a4~1
CROSS_ENV_HEAD = "bb5e2a484aac9b78e288dc9878d8e4295b35c3e1"

# -- Three clean precision anchors (small diffs verified bug-free) --
CLEAN_COMMENT_BASE = "b3dadb6e4abbddad4d58b0bddbfbaab4c17ebd5b"  # 1c0671c~1
CLEAN_COMMENT_HEAD = "1c0671c08f15030b8d7542dff119665e9d6472b6"  # 1-line comment edit
CLEAN_DOCSTRING_BASE = "96a443c16ef55c08509dcb26caa753b6fcc0c632"  # b3dadb6~1
CLEAN_DOCSTRING_HEAD = "b3dadb6e4abbddad4d58b0bddbfbaab4c17ebd5b"  # test docstring wording
CLEAN_PYTHONPATH_BASE = "5ec9ea1f372bbdf5a1373c2e0fcbff9934e02a36"  # b64fd60~1
CLEAN_PYTHONPATH_HEAD = "b64fd601cc80145cbcda0057b1e099dbb4754cdd"  # additive pyproject + test


def build() -> None:
    cross_env_leak = Fixture(
        id="cross-env-leak",
        source="curated",
        base_sha=CROSS_ENV_BASE,
        head_sha=CROSS_ENV_HEAD,
        diff=_diff(CROSS_ENV_BASE, CROSS_ENV_HEAD),
        # BEHAVIOR-NEUTRAL task_context: do NOT say "check all call sites" — that
        # cues the completeness slice and rigs the very comparison Q2 is testing.
        task_context="Inject the engine-managed env vars into the bash tool's subprocess.",
        expected_findings=(
            ExpectedFinding(
                # the SIBLING (un-changed) parallel path, in head-tree line space
                locus="src/imp/engine/gates.py:53",
                symbol="run_script_gate",
                description=(
                    "cross-cutting gap: the bash tool now spreads the full ambient "
                    "environment ({**os.environ, **engine_env(...)}); the sibling "
                    "subprocess-spawn site run_script_gate (gates.py) does the "
                    "identical os.environ.copy() merge, so an IMP_LINT_COMMAND "
                    "inherited from an outer imp process leaks into the child and "
                    "falsely enables the lint gate. Neither site strips inherited "
                    "engine-managed vars. A monolithic in-diff review of bash.py "
                    "misses the parallel gates.py path; a completeness pass that "
                    "greps every subprocess env-construction site catches it."
                ),
                severity="Major",
                must_catch=True,
            ),
        ),
        clean_loci=(),
        meta=FixtureMeta(
            "code-reviewer", None, True, True,
            stratum="cross-cutting", independence="independent-history",
        ),
    )

    def clean(fid: str, base: str, head: str) -> Fixture:
        return Fixture(
            id=fid, source="curated", base_sha=base, head_sha=head,
            diff=_diff(base, head),
            task_context="A small, correct change.",
            expected_findings=(),  # clean: NO labeled bugs -> any flag is a false positive
            clean_loci=(),
            meta=FixtureMeta(
                "code-reviewer", None, False, False,
                stratum="clean", independence="independent-history",
            ),
        )

    fixtures = [
        cross_env_leak,
        clean("clean-executor-comment", CLEAN_COMMENT_BASE, CLEAN_COMMENT_HEAD),
        clean("clean-test-docstring", CLEAN_DOCSTRING_BASE, CLEAN_DOCSTRING_HEAD),
        clean("clean-pytest-pythonpath", CLEAN_PYTHONPATH_BASE, CLEAN_PYTHONPATH_HEAD),
    ]
    for fx in fixtures:
        save_fixture(fx, OUT)
        print(f"wrote {OUT / fx.id}")


if __name__ == "__main__":
    build()
