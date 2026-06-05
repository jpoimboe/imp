"""`imp eval` subcommand. dry-run never bills (matrix + dollar estimate); run is
gated behind IMP_RUN_EVALS; scorecard re-renders from cache. The default Variant
set is a single baseline code-reviewer; richer matrices are configured by editing
the builder or passing a variants JSON (future)."""

from __future__ import annotations

import argparse
from pathlib import Path

from imp.engine.worktree import find_control_root
from imp.evals.fixtures import load_fixtures
from imp.evals.models import ExpectedFinding, Fixture, Variant
from imp.evals.pricing import PRICING_VERSION

_CURATED = Path("src/imp/evals/data/curated")


def _default_variants() -> list[Variant]:
    return [
        Variant(
            id="baseline-monolithic",
            role_to_model_map={"code-reviewer": ("anthropic", "claude-sonnet-4-6")},
            n_repeats=3,
        )
    ]


def _curated_fixtures() -> list[Fixture]:
    if _CURATED.exists():
        return load_fixtures(_CURATED)
    return []


def cmd_dry_run(args: argparse.Namespace) -> int:
    from imp.evals.sweep import plan_sweep

    plan = plan_sweep(
        _default_variants(), _curated_fixtures(),
        judge_provider=getattr(args, "judge_provider", "anthropic"),
        judge_model=getattr(args, "judge_model", "claude-opus-4-7"),
    )
    print(
        f"matrix: {plan.n_cells} cells, {plan.n_agent_runs} reviewer runs, "
        f"{plan.n_judge_calls} judge calls; estimated ${plan.estimated_dollars:.2f}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from imp.evals.provenance import provenance_digest, stamp
    from imp.evals.sweep import SweepError, run_sweep

    control_root = find_control_root(Path.cwd())
    variants = _default_variants()
    prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}
    cell_models = {v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in variants}
    run_stamp = stamp(
        prompt_dirs=prompt_dirs,
        cell_models=cell_models,
        judge_model=f"{args.judge_provider}/{args.judge_model}",
        pricing_version=PRICING_VERSION,
    )
    prov = provenance_digest(run_stamp)
    cache_dir = control_root / ".imp" / "eval-cache"
    try:
        grades = run_sweep(
            control_root,
            variants,
            _curated_fixtures(),
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

    fixture_list = _curated_fixtures()
    paired_model = ("anthropic", "claude-sonnet-4-6")
    q1, q4 = run_q1_q4(
        control_root,
        fixture_list,
        {
            "spec-task-reviewer": paired_model,
            "quality-reviewer": paired_model,
            "code-reviewer": paired_model,
        },
        judge_provider=args.judge_provider,
        judge_model=args.judge_model,
    )

    from imp.evals.scorecard import build_scorecard, render_markdown
    from imp.evals.sweep import collect_dollars

    fixtures = {f.id: f for f in fixture_list}
    sc = build_scorecard(
        grades=grades,
        fixtures=fixtures,
        dollars_by_cell=collect_dollars(cache_dir, variants, fixture_list, prov),
        prompt_dirs=prompt_dirs,
        provenance=run_stamp,
        paired_overlap=q1,
        fastpath=q4,
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


def _run_provenance(args: argparse.Namespace, variants: list[Variant]) -> str:
    from imp.evals.provenance import provenance_digest, stamp

    return provenance_digest(stamp(
        prompt_dirs={"baseline": Path("src/imp/assets/workflows/default/agents")},
        cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in variants},
        judge_model=f"{args.judge_provider}/{args.judge_model}",
        pricing_version=PRICING_VERSION,
    ))


def cmd_scorecard(args: argparse.Namespace) -> int:
    """Re-render the scorecard from CACHED reviewer runs — skips the expensive
    reviewer sweep (the cache's headline value). Grading still calls the judge, so
    it is gated like a sweep. Grades are not cached in v1."""
    from imp.evals.cache import load_run
    from imp.evals.grader import grade
    from imp.evals.scorecard import build_scorecard, render_markdown
    from imp.evals.sweep import collect_dollars, require_optin

    require_optin()  # re-grading calls the judge (billable)
    control_root = find_control_root(Path.cwd())
    variants = _default_variants()
    fixtures = _curated_fixtures()
    prov = _run_provenance(args, variants)
    cache_dir = control_root / ".imp" / "eval-cache"
    grades = []
    for v in variants:
        for fx in fixtures:
            for r in range(v.n_repeats):
                rr = load_run(cache_dir, v.id, fx.id, r, prov)
                if rr is None:
                    continue
                grades.append(grade(
                    fx, rr, project_root=control_root,
                    judge_provider=args.judge_provider, judge_model=args.judge_model,
                ))
    if not grades:
        print("no cached runs for this provenance; run `imp eval run` first")
        return 1
    sc = build_scorecard(
        grades=grades, fixtures={f.id: f for f in fixtures},
        dollars_by_cell=collect_dollars(cache_dir, variants, fixtures, prov),
        prompt_dirs={"baseline": Path("src/imp/assets/workflows/default/agents")},
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
    return 0


def add_eval_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("eval", help="Run the review-evaluation harness")
    actions = p.add_subparsers(dest="action", required=True)

    pd = actions.add_parser("dry-run", help="Report the run matrix + dollar estimate (never bills)")
    pd.set_defaults(func=cmd_dry_run)

    pr = actions.add_parser("run", help="Run the gated sweep and write a scorecard")
    pr.add_argument("--judge-provider", default="anthropic")
    pr.add_argument("--judge-model", default="claude-opus-4-7")
    pr.add_argument("--concurrency", type=int, default=4)
    pr.set_defaults(func=cmd_run)

    pm = actions.add_parser("mine", help="Mine .claude session logs into fixtures")
    _default_glob = "/home/jpoimboe/.claude/projects/-home-jpoimboe-imp/*.jsonl"
    pm.add_argument("--corpus-glob", default=_default_glob)
    pm.add_argument("--out", default="src/imp/evals/data/mined")
    pm.add_argument("--judge-provider", default="anthropic")
    pm.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    pm.set_defaults(func=cmd_mine)

    ps = actions.add_parser(
        "scorecard", help="Re-render the scorecard from cached runs (no reviewer sweep)"
    )
    ps.add_argument("--judge-provider", default="anthropic")
    ps.add_argument("--judge-model", default="claude-opus-4-7")
    ps.set_defaults(func=cmd_scorecard)

    pc = actions.add_parser("calibrate", help="Run the judge calibration hop on judge_items.json")
    pc.add_argument("--judge-items", default="src/imp/evals/data/calibration/judge_items.json")
    pc.add_argument("--judge-provider", default="openai")  # cross-vendor from Anthropic reviewers
    pc.add_argument("--judge-model", default="gpt-5.2")
    pc.set_defaults(func=cmd_calibrate)
