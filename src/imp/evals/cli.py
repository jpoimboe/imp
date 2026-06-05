"""`imp eval` subcommand. dry-run never bills (matrix + dollar estimate); run is
gated behind IMP_RUN_EVALS; scorecard re-renders from cache. The default Variant
set is a five-model matrix (OpenAI gpt-5.5/gpt-5.3-codex/gpt-5.4-mini + claude_code
opus-4-8/sonnet-4-6), one Variant per model binding the `code-reviewer` role, 3
repeats each."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from imp.engine.worktree import find_control_root
from imp.evals.fixtures import load_fixtures
from imp.evals.mining import AGGREGATE_BLOCKING_FLOOR as _AGG
from imp.evals.mining import PER_QUESTION_BLOCKING_FLOOR as _PERQ
from imp.evals.mining import FeasibilityCount
from imp.evals.models import ExpectedFinding, Fixture, Variant
from imp.evals.pricing import PRICING_VERSION

_CURATED = Path("src/imp/evals/data/curated")
_MINED = Path("src/imp/evals/data/mined")
_DEFAULT_CORPUS_GLOB = "/home/jpoimboe/.claude/projects/-home-jpoimboe-imp/*.jsonl"

# Stage 2 (M3b): paired Q1/Q4 runs once per OpenAI model (each call binds all
# three reviewer roles to that model). The claude_code pair is read on the Q3
# RECALL axis only ($0 distorts the $-normalized ranking) and is not paired here.
_PAIRED_MODELS = [
    ("openai", "gpt-5.5"),
    ("openai", "gpt-5.3-codex"),
    ("openai", "gpt-5.4-mini"),
]


def _resolve_sha_mismatch(
    *, runs_shas: set[str], head: str, reuse_stale: bool, is_tty: bool
) -> str:
    """Decide continue/abort for cmd_scorecard when cached cells were produced at a
    different imp SHA than HEAD. Binary (grades are never cached, so we always
    re-grade on continue). Non-interactive or --yes defaults to abort;
    --reuse-stale-sha pre-answers continue; no mismatch -> continue silently."""
    stale = {s for s in runs_shas if s and s != head}
    if not stale:
        return "continue"
    if reuse_stale:
        return "continue"
    if not is_tty:
        return "abort"
    print(
        f"\n⚠ cached reviewer runs were produced at imp {sorted(stale)}, but HEAD is {head}.\n"
        "  Prompts, models, judge, and pricing are unchanged — but code outside those\n"
        "  (the eval harness, parsing, tool schema) may have changed; review if you\n"
        "  touched the harness.\n"
        "  [c]ontinue (grade the cached runs)   [a]bort"
    )
    answer = input("> ").strip().lower()
    return "continue" if answer.startswith("c") else "abort"


def _default_variants() -> list[Variant]:
    """The reviewer model matrix: one Variant per model, each binding the single
    `code-reviewer` role at the single-agent seam. The OpenAI trio is priced (real
    Q3 $-axis); the claude_code pair runs on the subscription at $0 and is read on
    the RECALL axis only (a $0 model looks infinitely cost-efficient on Q3).

    Variant ids use a `mono-` prefix, NOT the literal `monolithic`/`completeness`
    substrings Q2 selects on (scorecard.py:72-73) — so these model-matrix cells
    stay out of the Q2 structure comparison by construction. Q2's structure
    variants are Stage 3's job.
    """
    def _mono(vid: str, provider: str, model: str) -> Variant:
        return Variant(id=vid, role_to_model_map={"code-reviewer": (provider, model)}, n_repeats=3)

    return [
        _mono("mono-gpt-5.5", "openai", "gpt-5.5"),
        _mono("mono-gpt-5.3-codex", "openai", "gpt-5.3-codex"),
        _mono("mono-gpt-5.4-mini", "openai", "gpt-5.4-mini"),
        _mono("mono-cc-opus-4-8", "claude_code", "claude-opus-4-8"),
        _mono("mono-cc-sonnet-4-6", "claude_code", "claude-sonnet-4-6"),
    ]


def _structure_variants() -> list[Variant]:
    """The Stage 3 granularity 2x2: {per-task, whole-change} x {monolithic, slice}.
    `baseline-monolithic` is the whole-change-mono cell (also Q2's monolithic pop.);
    `whole-change-completeness` is the whole-change-slice cell (Q2's slice pop.). The
    per-task pair carries NEITHER Q2 substring, so it stays out of Q2. The slice cells
    add the baseline `completeness-slice` agent as a second panel member (no
    prompt_overrides needed). Per-task cells run extra repeats for power."""
    reviewer = ("anthropic", "claude-sonnet-4-6")
    slice_member = ("anthropic", "claude-sonnet-4-6")
    return [
        Variant(
            id="baseline-monolithic",
            role_to_model_map={"code-reviewer": reviewer},
            structure="monolithic", n_repeats=3,
        ),
        Variant(
            id="whole-change-completeness",
            role_to_model_map={"code-reviewer": reviewer, "completeness-slice": slice_member},
            structure="slice-panel", n_repeats=3,
        ),
        Variant(
            id="per-task-mono",
            role_to_model_map={"code-reviewer": reviewer},
            structure="per-task-monolithic", n_repeats=5,
        ),
        Variant(
            id="per-task-slice",
            role_to_model_map={"code-reviewer": reviewer, "completeness-slice": slice_member},
            structure="per-task-slice-panel", n_repeats=5,
        ),
    ]


def _curated_fixtures() -> list[Fixture]:
    if _CURATED.exists():
        return load_fixtures(_CURATED)
    return []


def _mined_fixtures() -> list[Fixture]:
    if _MINED.exists():
        return load_fixtures(_MINED)
    return []


def cmd_dry_run(args: argparse.Namespace) -> int:
    from imp.evals.sweep import plan_paired, plan_sweep

    judge_provider = getattr(args, "judge_provider", "anthropic")
    judge_model = getattr(args, "judge_model", "claude-opus-4-8")
    plan = plan_sweep(
        _default_variants() + _structure_variants(), _curated_fixtures(),
        judge_provider=judge_provider, judge_model=judge_model,
    )
    print(
        f"matrix: {plan.n_cells} cells, {plan.n_agent_runs} reviewer runs, "
        f"{plan.n_judge_calls} judge calls; estimated ${plan.estimated_dollars:.2f}"
    )
    # The paired Q1/Q4 path (Stage 2): OpenAI trio x (mined + curated) fixtures,
    # UNCACHED, so it re-bills every run and is invisible to plan_sweep above.
    paired = plan_paired(
        _PAIRED_MODELS, _mined_fixtures() + _curated_fixtures(),
        judge_provider=judge_provider, judge_model=judge_model,
    )
    print(
        f"paired Q1/Q4 (UNCACHED — re-billed each run): {paired.n_paired_models} models, "
        f"{paired.n_reviewer_runs} reviewer runs, {paired.n_judge_calls} judge calls; "
        f"estimated ${paired.estimated_dollars:.2f}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from imp.evals.provenance import core_digest, stamp
    from imp.evals.provenance import fixture_label_hash as _flh
    from imp.evals.sweep import SweepError, run_sweep

    control_root = find_control_root(Path.cwd())
    variants = _default_variants() + _structure_variants()
    fixtures_for_run = _curated_fixtures()
    prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}
    cell_models = {v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in variants}
    label_hash = _flh(fixtures_for_run)
    run_stamp = stamp(
        prompt_dirs=prompt_dirs,
        cell_models=cell_models,
        judge_model=f"{args.judge_provider}/{args.judge_model}",
        pricing_version=PRICING_VERSION,
        fixture_label_hash=label_hash,
    )
    prov = core_digest(run_stamp)
    cache_dir = control_root / ".imp" / "eval-cache"
    try:
        grades = run_sweep(
            control_root,
            variants,
            fixtures_for_run,
            cache_dir=cache_dir,
            judge_provider=args.judge_provider,
            judge_model=args.judge_model,
            concurrency=args.concurrency,
            provenance=prov,
        )
    except SweepError as e:
        print(str(e))
        return 2
    from imp.evals.analysis import run_q1_q4

    # Q3 sweep + scorecard stay on the CURATED set; the paired Q1/Q4 pool ADDS the
    # mined diffs (mined+curated) so n_diffs is non-trivial. Both verdicts still
    # read insufficient-diffs while n_diffs < min_diff_count()=93 (spec M-c: we do
    # NOT lower the gate); the RAW numbers rendered beside them are the read.
    #
    # The paired path is UNCACHED (run_paired has no cache) and re-bills every run —
    # the dominant recurring Stage 2 cost. --skip-paired runs ONLY the cached Q3
    # sweep, so routine Q3 re-renders don't drag it along. When skipped, Q1/Q4
    # render the empty "insufficient-diffs (no paired runs supplied)" path.
    q1_by_model: dict[str, Any] = {}
    q4_by_model: dict[str, Any] = {}
    if not getattr(args, "skip_paired", False):
        paired_fixtures = _mined_fixtures() + fixtures_for_run
        for provider, model in _PAIRED_MODELS:
            q1, q4 = run_q1_q4(
                control_root,
                paired_fixtures,
                {role: (provider, model) for role in
                 ("spec-task-reviewer", "quality-reviewer", "code-reviewer")},
                judge_provider=args.judge_provider,
                judge_model=args.judge_model,
            )
            q1_by_model[f"{provider}/{model}"] = q1
            q4_by_model[f"{provider}/{model}"] = q4

    from imp.evals.scorecard import build_scorecard, render_markdown
    from imp.evals.sweep import collect_dollars

    fixtures = {f.id: f for f in fixtures_for_run}
    sc = build_scorecard(
        grades=grades,
        fixtures=fixtures,
        dollars_by_cell=collect_dollars(cache_dir, variants, fixtures_for_run, prov),
        prompt_dirs=prompt_dirs,
        provenance=run_stamp,
        paired_overlap=q1_by_model,
        fastpath=q4_by_model,
    )
    out = control_root / ".imp" / "eval-scorecard.md"
    out.write_text(render_markdown(sc))
    print(f"scorecard -> {out}")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    import glob

    from imp.evals.mining import mine_corpus
    from imp.evals.normalize import normalize_findings

    control_root = find_control_root(Path.cwd())
    files = [Path(p) for p in glob.glob(args.corpus_glob)]

    def _normalize(prose: str) -> tuple[ExpectedFinding, ...]:
        return normalize_findings(
            prose, project_root=Path.cwd(), provider=args.judge_provider, model=args.judge_model
        )

    report = mine_corpus(files, Path(args.out), control_root=control_root, normalize=_normalize)
    print(
        f"episodes={report.episodes_found} written={report.fixtures_written} "
        f"drops={report.drops_by_reason} thin={report.thin}"
    )
    return 0


def _render_feasibility(c: FeasibilityCount) -> str:
    """Human-readable go/no-go. PROCEED/STOP is advisory — the human decides; this
    is the cheap 'look before you invest' gate, read per question (spec F1/F3)."""
    def _mark(ok: bool) -> str:
        return "PROCEED" if ok else "STOP"

    return (
        "feasibility (distinct blocking-bearing diffs; free pre-count):\n"
        f"  aggregate: {c.aggregate_blocking_diffs} "
        f"(floor {_AGG}) -> {_mark(c.aggregate_ok)}\n"
        f"  Q1 (drop spec-task-reviewer): {c.q1_blocking_diffs} "
        f"(floor {_PERQ}) -> {_mark(c.q1_ok)}\n"
        f"  Q4 (drop code-reviewer): {c.q4_blocking_diffs} "
        f"(floor {_PERQ}) -> {_mark(c.q4_ok)}\n"
        "note: proxy counts diffs (head_shas), is mined-only, and is a LOWER bound "
        "on the grade's n (which also pools curated fixtures). Read each question "
        "by its own count."
    )


def cmd_feasibility(args: argparse.Namespace) -> int:
    """Component 0: free pre-count of distinct blocking-bearing diffs per question
    before any billable mining/sweep. Never calls a provider; always exits 0."""
    import glob

    from imp.evals.mining import count_corpus_feasibility

    files = [Path(p) for p in glob.glob(args.corpus_glob)]
    c = count_corpus_feasibility(files)
    print(_render_feasibility(c))
    return 0


def _run_provenance(args: argparse.Namespace, variants: list[Variant],
                    fixtures: list[Fixture]) -> str:
    from imp.evals.provenance import core_digest, fixture_label_hash, stamp

    return core_digest(stamp(
        prompt_dirs={"baseline": Path("src/imp/assets/workflows/default/agents")},
        cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in variants},
        judge_model=f"{args.judge_provider}/{args.judge_model}",
        pricing_version=PRICING_VERSION,
        fixture_label_hash=fixture_label_hash(fixtures),
    ))


def cmd_scorecard(args: argparse.Namespace) -> int:
    """Re-render the scorecard from CACHED reviewer runs — skips the expensive
    reviewer sweep (the cache's headline value). Grading still calls the judge, so
    it is gated like a sweep. Grades are not cached in v1."""
    from imp.evals.cache import load_run
    from imp.evals.grader import grade
    from imp.evals.provenance import imp_commit_sha, stamp
    from imp.evals.scorecard import build_scorecard, render_markdown
    from imp.evals.sweep import collect_dollars, require_optin

    require_optin()  # re-grading calls the judge (billable)
    control_root = find_control_root(Path.cwd())
    variants = _default_variants() + _structure_variants()
    fixtures = _curated_fixtures()
    prov = _run_provenance(args, variants, fixtures)
    cache_dir = control_root / ".imp" / "eval-cache"

    # First pass: load cells, collect their stored SHAs (no grading yet).
    loaded = []
    runs_shas: set[str] = set()
    for v in variants:
        for fx in fixtures:
            for r in range(v.n_repeats):
                rr = load_run(cache_dir, v.id, fx.id, r, prov)
                if rr is None:
                    continue
                loaded.append((fx, rr))
                if rr.imp_commit_sha:
                    runs_shas.add(rr.imp_commit_sha)
    if not loaded:
        print("no cached runs for this provenance; run `imp eval run` first")
        return 1

    head = imp_commit_sha()
    decision = _resolve_sha_mismatch(
        runs_shas=runs_shas, head=head,
        reuse_stale=getattr(args, "reuse_stale_sha", False),
        is_tty=sys.stdin.isatty() and not getattr(args, "yes", False),
    )
    if decision == "abort":
        print("aborted: cached runs were produced at a different imp SHA than HEAD")
        return 2

    grades = [
        grade(fx, rr, project_root=control_root,
              judge_provider=args.judge_provider, judge_model=args.judge_model)
        for fx, rr in loaded
    ]

    # Full stamp (with HEAD SHA) for the provenance block ONLY; runs_shas records
    # where the cached cells actually came from. fixture_label_hash is omitted here
    # deliberately — this stamp is for DISPLAY, and render_markdown never renders
    # that field (only imp_commit_sha / prompt_hashes / runs_shas). The cache-key
    # stamp that DOES need it is `_run_provenance` (above), unchanged.
    prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}
    run_stamp = stamp(
        prompt_dirs=prompt_dirs,
        cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()]
                     for v in variants},
        judge_model=f"{args.judge_provider}/{args.judge_model}",
        pricing_version=PRICING_VERSION,
    )
    sc = build_scorecard(
        grades=grades, fixtures={f.id: f for f in fixtures},
        dollars_by_cell=collect_dollars(cache_dir, variants, fixtures, prov),
        prompt_dirs=prompt_dirs,
        provenance=run_stamp,
        runs_shas=runs_shas,
    )
    out = control_root / ".imp" / "eval-scorecard.md"
    out.write_text(render_markdown(sc))
    print(f"scorecard (from cache) -> {out}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Run the judge calibration hop on a human-labeled judge_items.json and
    report agreement + Cohen's kappa per output-style (billable: calls the judge)."""
    import json

    from imp.evals.calibration import CalibrationItem, calibrate_judge_hop
    from imp.evals.sweep import require_optin

    require_optin()
    items = [CalibrationItem(**d) for d in json.loads(Path(args.judge_items).read_text())]
    report = calibrate_judge_hop(
        items, project_root=Path.cwd(),
        judge_provider=args.judge_provider, judge_model=args.judge_model,
    )
    print(f"pooled: agree={report.pooled.agreement:.3f} kappa={report.pooled.kappa} "
          f"ci={report.pooled.ci} n={report.pooled.n} floor_met={report.floor_met}")
    for style, ag in report.by_style.items():
        print(f"  {style}: agree={ag.agreement:.3f} kappa={ag.kappa} n={ag.n}")
    if not report.floor_met:
        print("WARNING: calibration floor unmet — scorecard verdicts are NOT trustworthy yet")

    norm_path = Path(args.normalization_items)
    if norm_path.exists():
        from imp.evals.calibration import (
            NormalizationItem,
            calibrate_normalization_hop,
        )

        norm_items = [
            NormalizationItem.from_dict(d) for d in json.loads(norm_path.read_text())
        ]
        # Normalize with the MINING normalizer (default haiku), NOT the opus judge —
        # hop-2 must calibrate the model that actually labels the fixtures.
        norm = calibrate_normalization_hop(
            norm_items, project_root=Path.cwd(),
            normalizer_provider=args.normalizer_provider,
            normalizer_model=args.normalizer_model,
        )
        print(
            f"normalization hop (normalizer {args.normalizer_provider}/"
            f"{args.normalizer_model}; report-only; eyeball disagreements, no gate):"
        )
        def _fmt(v: float | None) -> str:
            return f"{v:.3f}" if v is not None else "n/a (no matched findings)"

        for style, nag in norm.by_style.items():
            print(
                f"  {style}: identity_f1={nag.identity_f1:.3f} "
                f"severity={_fmt(nag.severity_agreement)} "
                f"must_catch={_fmt(nag.must_catch_agreement)} n={nag.n}"
            )
    else:
        print(f"(no normalization_items at {norm_path}; skipping hop-2)")
    return 0


def add_eval_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("eval", help="Run the review-evaluation harness")
    actions = p.add_subparsers(dest="action", required=True)

    pd = actions.add_parser("dry-run", help="Report the run matrix + dollar estimate (never bills)")
    pd.set_defaults(func=cmd_dry_run)

    pr = actions.add_parser("run", help="Run the gated sweep and write a scorecard")
    pr.add_argument("--judge-provider", default="anthropic")
    pr.add_argument("--judge-model", default="claude-opus-4-8")
    pr.add_argument("--concurrency", type=int, default=4)
    pr.add_argument("--skip-paired", action="store_true",
                    help="run only the cached Q3 sweep; skip the uncached, "
                         "re-billed-every-run Q1/Q4 paired path")
    pr.set_defaults(func=cmd_run)

    pm = actions.add_parser("mine", help="Mine .claude session logs into fixtures")
    pm.add_argument("--corpus-glob", default=_DEFAULT_CORPUS_GLOB)
    pm.add_argument("--out", default="src/imp/evals/data/mined")
    pm.add_argument("--judge-provider", default="anthropic")
    pm.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    pm.set_defaults(func=cmd_mine)

    pf = actions.add_parser(
        "feasibility",
        help="Free pre-count of blocking-bearing diffs per question (never bills)",
    )
    pf.add_argument("--corpus-glob", default=_DEFAULT_CORPUS_GLOB)
    pf.set_defaults(func=cmd_feasibility)

    ps = actions.add_parser(
        "scorecard", help="Re-render the scorecard from cached runs (no reviewer sweep)"
    )
    ps.add_argument("--judge-provider", default="anthropic")
    ps.add_argument("--judge-model", default="claude-opus-4-8")
    ps.add_argument("--reuse-stale-sha", action="store_true",
                    help="grade cached cells even if produced at a different imp SHA")
    ps.add_argument("--yes", action="store_true",
                    help="non-interactive: do not prompt (defaults to abort on SHA mismatch)")
    ps.set_defaults(func=cmd_scorecard)

    pc = actions.add_parser("calibrate", help="Run the judge calibration hop on judge_items.json")
    pc.add_argument("--judge-items", default="src/imp/evals/data/calibration/judge_items.json")
    pc.add_argument("--judge-provider", default="anthropic")
    pc.add_argument("--judge-model", default="claude-opus-4-8")  # the calibrated judge (README)
    pc.add_argument(
        "--normalization-items",
        default="src/imp/evals/data/calibration/normalization_items.json",
        help="when this file exists, calibrate ALSO runs the billable hop-2 "
             "normalizer (not just the judge hop); point at a missing path to skip",
    )
    pc.add_argument("--normalizer-provider", default="anthropic")
    pc.add_argument("--normalizer-model", default="claude-haiku-4-5-20251001",
                    help="must match `imp eval mine`'s --judge-model (the normalizer "
                         "that labels fixtures), NOT the opus judge")
    pc.set_defaults(func=cmd_calibrate)
