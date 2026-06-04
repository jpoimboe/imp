# imp review-evaluation harness

Measures how well imp's **real review agents** catch real bugs, so changes to the
review system (drop a reviewer? panel vs monolithic? which model? fast-path?) are
decided by evidence instead of vibes.

It runs the actual production seam — `build_agent_invocation → provider.create_runner
→ drive_invocation` — against labeled fixtures, has an LLM judge decide whether each
review surfaced each planted bug, and scores recall / precision / dollars. The judge
is itself **calibrated against human labels** (Cohen's κ) so its numbers are
trustworthy.

> **Status (2026-06):** offline machinery complete and tested. Judge calibrated at
> κ≈0.67 (substantial). The default sweep answers **Q3 for one baseline model**;
> Q1/Q2/Q4 need more variants/fixtures to move off *insufficient* (see Roadmap).

---

## Quickstart

From the repo root:

```bash
export PYTHONPATH=src                              # or use the installed `imp`
export IMP_RUN_EVALS=1                             # gate: required for any billable run
export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key)   # anthropic reviewers + the judge
export OPENAI_API_KEY=$(cat ~/.openai-key)         # only if you use openai reviewers
# claude_code reviewers need no key (use the Claude subscription); they price as $0
```

| Command | Cost | What it does |
|---|---|---|
| `imp eval dry-run` | **free** | Print the run matrix + dollar estimate. Start here. |
| `imp eval run --judge-provider anthropic --judge-model claude-opus-4-8` | $$ | Full sweep → grade → write `.imp/eval-scorecard.md` (Q1–Q4). |
| `imp eval scorecard --judge-provider anthropic --judge-model claude-opus-4-8` | $ | Re-render the scorecard from **cached** reviewer runs (re-grades only; skips the reviewer sweep). |
| `imp eval calibrate --judge-provider anthropic --judge-model claude-opus-4-8` | $ | Judge-hop agreement + κ vs `data/calibration/judge_items.json`. Re-run whenever you change the judge. |
| `imp eval mine --out src/imp/evals/data/mined` | $ | Mine real `.claude` session logs into new fixtures. |

**Judge model:** use `claude-opus-4-8` — that's what the current calibration validated.
The harness **hard-fails** if the judge's exact `(provider, model)` equals any reviewer
under test (oracle independence); a same-vendor / different-model judge is a soft warning.

---

## The four questions

The scorecard is organized around four pre-registered decisions (`decisions.py` holds
the thresholds; verdicts are computed, not hand-waved):

- **Q1 — drop `spec-task-reviewer`?** Its *unique blocking* contribution vs the union of
  the other two reviewers, on paired runs over the same diff.
- **Q2 — structure: monolithic vs completeness-slice panel?** Does a panel that greps
  every caller catch cross-cutting bugs a single whole-change pass misses?
- **Q3 — model/provider matrix, $-normalized.** Recall per model per dollar.
- **Q4 — can a fast per-change path replace per-task review?** Fast-path blocking recall
  vs the union's.

Each question returns a real verdict or an honest `insufficient-diffs` / `insufficient-data`
when the curated set is too small to support it — that's the designed answer, not a gap.

---

## How it works

```
fixture (labeled diff)
   └─ materialize_fixture_tree  → disposable git worktree with the diff at HEAD
        └─ run_variant / run_review  → drive the REAL reviewer agent(s) (drive_agent)
             └─ parse_review_findings → structured findings
                  └─ grade (LLM judge) → match_kind per expected finding (exact/partial/none)
                       └─ aggregate over repeats → recall / blocking_recall / precision + Wilson CI
                            └─ build_scorecard → Q1–Q4 → .imp/eval-scorecard.md
```

Key pieces:
- **Fixtures** (`data/curated/`, `data/mined/`): a base/head SHA, the diff, task context,
  and **expected findings** (symbol, locus, description, severity) + **clean loci**
  (false-positive bait). Strata today: `in-diff`, `deep-tail`, `cross-cutting`, `clean`.
- **Variants** (`structures.py`): `monolithic` = one reviewer; `slice-panel` = N distinct
  reviewer agents whose findings are de-duped by the shared `match_kind`. The
  **completeness slice** is a panel member with an override prompt that only hunts
  callers / parallel paths (`data/agents/completeness-slice.md`).
- **Matching** (`matching.py`): symbol-first, line-as-hint. `exact` = right symbol +
  mechanism, `partial` = right bug wrong line/rationale, `none` = not surfaced / mere mention.
- **Cost** (`pricing.py`): per-(provider,model) list prices → dollars per cell. `claude_code`
  is unpriced ($0); a real anthropic/openai sweep prices Q3.
- **Provenance** (`provenance.py`): every result is stamped with the imp commit SHA,
  prompt-dir hashes, model ids, judge model, and pricing version. The cache refuses to
  serve a cell under a different stamp.

### The judge, and why it's calibrated

The judge is an LLM (`data/agents/grader-judge.md`) that decides catch/miss. LLM judges
are lenient, and that leniency is **confounded with the variable under test**. So we
calibrate it against human ground truth (`calibration.py`):

- **Hop 1 (judge):** for each human-labeled `(finding, review)` pair in
  `data/calibration/judge_items.json`, does the judge agree with the human? Reported as
  agreement + **Cohen's κ, per output-style** (so a style-correlated bias can't hide in a
  pooled number). Current: pooled κ≈0.67, claude_code 0.74, openai 0.60.
- **Hop 2 (normalization):** does the normalizer recover the human's structured findings
  from raw prose? Designed (`NormalizationItem`, `normalization_agreement`) but **not yet
  wired to the CLI** and `normalization_items.json` is **not yet authored** — see Roadmap.

Label sets must reach ≥20 items and ≥10 per style, spanning ≥2 providers. Until the floor
is met, `calibrate` reports `floor_met=False` and the scorecard verdicts are not trustworthy.

---

## Gotchas (these have bitten us)

1. **Provenance pins the imp commit SHA**, so *any* new commit — even docs — invalidates
   the result cache (`imp eval scorecard` then says "no cached runs for this provenance").
   Run a sweep on a **stable HEAD** and render its scorecard before committing other work.
2. **The default config is one model, one structure** (`_default_variants()` in `cli.py`:
   `baseline-monolithic`, `claude-sonnet-4-6`, 3 repeats). It answers Q3 for that model and
   honestly declines Q1/Q2/Q4. Widen it to make Q2/Q3 real comparisons.
3. **`dry-run`'s estimate covers only the main sweep.** The Q1/Q4 paired runs (3 reviewers ×
   fixtures) add cost on top.
4. **Every eval-agent run gets a throwaway event log outside its worktree** (`driver.py`).
   Don't "optimize" that away: a shared/persistent log makes the provider *resume a stale
   transcript* (judge silently returns `none` for everything), and a log inside the
   disposable worktree vanishes mid-run (paired-run teardown → `FileNotFoundError`).

---

## Roadmap — what comes next

**Make the existing questions real (no new concepts, just more inputs):**
- **Widen `_default_variants()`** to add (a) a `completeness-slice` panel variant and (b) a
  second model (e.g. `openai`/`gpt-5.2`). Then Q2 becomes monolithic-vs-slice and Q3 becomes
  an actual matrix instead of a single baseline.
- **Grow the curated set** past the decision-rule floors (more `cross-cutting` and
  `deep-tail` fixtures), or turn the **136 reconstructable mined episodes** into fixtures
  via one billable normalizer pass.
- **Author `normalization_items.json`** and wire hop 2 into `imp eval calibrate` so the
  *normalizer* is calibrated too, not just the judge.
- **Re-grade is billable; cache it.** Grades aren't cached in v1 — `scorecard` re-judges
  every time. A grade cache keyed by (run digest, judge) would make re-renders free.

**Sharpen the weak fixture** the calibration already flagged: `anyio-sleep-uncapped`'s
finding *description* ("second un-enumerated caller") doesn't cohere with its *locus*
(`subagents.py:203`, the in-diff sleep). All 3 human↔judge disagreements live there.

---

## What else we can measure

The harness is a general "run reviewers over labeled diffs and score them" engine; the four
questions are just the first batch. Things worth adding:

### Per-task monolithic vs sliced reviews  ← *next big one*
Today a fixture is **one whole change** (base→head) reviewed once. But imp reviews at two
granularities: **per-task** (each implemented task/commit, cheap and frequent) and
**whole-change** (the final completeness pass). The open question is *where bugs are best
caught*:
- **per-task monolithic** — review each task's diff in isolation. Catches local bugs early
  and cheaply, but is **blind to cross-task / cross-cutting issues** (the second caller in
  another task, the sibling subprocess site) because it never sees the other tasks.
- **per-task sliced** — add a completeness slice per task that greps the *whole tree* for
  callers/parallel paths of that task's changed symbols.
- **whole-change slice** — one completeness pass at the end over the cumulative diff.

To measure this we need **multi-task fixtures**: a fixture that is a *sequence* of N task
diffs plus the cumulative diff, with findings labeled by *which task introduced them* and
whether they're *local* or *cross-task*. Then we can score, per dollar:
recall(per-task monolithic) vs recall(per-task sliced) vs recall(whole-change slice),
split by local vs cross-task findings. The hypothesis worth falsifying: per-task review
catches local bugs but **cross-task recall collapses without a slice** — and the slice is
cheapest applied once at the end, not per task. This is Q2 + Q4 at task granularity and is
the natural next fixture format.

### Other dimensions
- **Acted-on recall** (caught *and* routed to fix) vs plain recall. A review that detects a
  Major bug and approves it scores identically to one that blocks today — the
  `review_finding.blocking` signal to separate them already exists; it just isn't a scorecard
  metric. (This is a real blind spot, noted during labeling.)
- **Precision / false-positive rate** on the `clean` fixtures — does a reviewer invent bugs?
  Partially wired (clean loci as bait); deserves its own scorecard line.
- **Severity calibration** — when a review catches a bug, does it assign the right severity?
  (Distinct from catch/miss.)
- **Stability / variance** across repeats — a reviewer that catches a bug 1-in-3 (like the
  baseline on `anyio`) is materially different from a reliable one; report variance, not just
  the mean.
- **Latency / wall-clock**, already captured in telemetry, as a comparison axis alongside $.
- **Prompt ablations** — drop the "read the spec/plan/progress" step, the web-search tool,
  etc., and measure the recall delta. The variant machinery already supports prompt overrides.
- **Context-fidelity sensitivity** — the harness injects one paragraph of task context, far
  less than production's full spec+plans+progress. Vary how much context the reviewer gets and
  measure how recall moves (bounds how much these numbers transfer to production).
