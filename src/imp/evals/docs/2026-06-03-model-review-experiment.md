# imp live-run-polish session — time analysis + model/review experiments

> **Why this is in the repo (provenance):** this is the throwaway, hand-run experiment
> that the review-eval harness (`src/imp/evals/`) was built to productize and harden.
> §6 here — an ad-hoc `bash`-tool review harness in `/tmp/haiku-test/`, gap-detection by
> the author's own grep of each review — is the literal prototype of the LLM-judge +
> fixture design. §7's recommendations became the harness's four questions (see
> `../README.md` → "From experiment to framework"). It is preserved verbatim as the
> evidence base; its explicit caveats (n small per cell, cross-provider scaffolding
> differs, prompt-strength confounds, a leaked-answer batch discarded) are exactly the
> rigor gaps the framework adds calibration + pre-registered thresholds to close.
> Treat the numbers as the original n=1-ish signal, **not** as harness output.

_Analysis date: 2026-06-03. Subject: the long-running `claude` session (PID 631038),
started 09:36 PDT, ~3h32m wall clock, executing the `imp-live-run-polish` plan. This
doc was reconstructed after accidental deletion; numbers are from the session
transcripts and the experiment result files in `/tmp/haiku-test/results/`._

---

## 1. Where the 3h32m went

Session = `/superpowers:brainstorming` → plan → execute, across three
compaction-linked transcripts (`772a5ea2` → `cdb8d7b1` → `f60c54a3`).

| Phase | Window | Duration | Content |
|---|---|---|---|
| A — Discuss + Plan | 0–80m | 80m43s | brainstorming, 4× AskUserQuestion (7m41s human wait), 2 review-doc rounds |
| B — Execution | 80m–end | 131m47s | 47 subagents implementing/reviewing a 12-task plan |

- **47 `Agent` subagent calls = 103m36s of compute = 49% of the whole session.**
- **Max concurrent agents = 1.** Union coverage == sum of durations: every subagent ran
  strictly sequentially, zero parallelism despite independent per-task work.
- All 49 agents ran on `model: sonnet`. Main-thread tools were negligible
  (73 Bash = 41s, 62 Read = 15s, 33 Edit = 15s).

Per-task pipeline (applied to all 12 tasks, serially):
**Implement → Spec-review → Code-review → Fix.**
- implement: 12 agents, 40m53s
- code-review: 11 agents, 34m53s
- spec-review: 13 agents, 14m30s
- fix/polish: 11 agents, ~13m

**Biggest single lever: the 1-at-a-time execution.** Fanning the independent tasks out
(~10 concurrent) would collapse ~104m of serial agent time toward the longest single
chain (~15–20m) — a >1h saving on the execution phase alone.

---

## 2. Implement experiment — re-running tasks under haiku / sonnet / opus

Re-ran tasks in isolated git worktrees from the same parent commits, with the identical
implement prompts the original session used. "Heavy" = high original token spend.

### Task 10 (§6 feedback-edge refactor) — a genuinely bounded task
| Model | Wall | Tokens | Tool calls | Files | `make check` | Outcome |
|---|---|---|---|---|---|---|
| sonnet (orig) | 7m21s | 92.6K | 67 | 12 | green | complete |
| opus | 6m22s | 67.2K | 69 | 12 (identical set) | green | complete |
| haiku | 10m02s | 76.2K | 140 | 12 (identical set) | green | complete |

Model-invariant: all three produced an identical file set and a correct implementation.
Haiku was decisively slowest (~2× tool churn). CAVEAT: n=1, sonnet from the original
session under different load; only "haiku slowest" is robust. Token count ≠ $ cost
(haiku cheapest per token, opus most expensive).

### Task 12 (§10 transient-5xx retry overhaul) — has a hidden cross-cutting gap
| Model | Wall | Tokens | Tool calls | Files | `make check` | Caught subagent gap? |
|---|---|---|---|---|---|---|
| sonnet (orig *implement*) | 5m51s | 53.3K | 51 | 7 | green | NO |
| opus | 5m31s | 56.4K | 45 | 7 | green | NO |
| haiku | 9m47s | 66.7K | 93 | 8 | green | NO |

**No implement model caught the gap.** The prompt scoped the work to the backoff loop in
`executor.py::_route_agent` and never mentioned the *second* backoff loop in
`agents/subagents.py::dispatch_task` — which still did a raw, now-unbounded
`anyio.sleep(result.delay)` on the hot path where API calls actually happen
(`subagents.py:209`). All three left it untouched and all passed `make check` (the tests
that would catch it didn't exist yet).

### What actually caught the gap in the original run
The 15-file "sonnet" §10 commit was really **implement (7 files) + a code-review-driven
fix (8 more):**
1. Implement (sonnet) → 7 files, executor-only. Self-review: "No concerns."
2. **Spec-review → rubber-stamp** ("only the 7 intended files changed, commit msg matches") — sat on top of the regression.
3. **Code-review → caught it** ("the one real regression is the subagents.py raw anyio.sleep… bounded before (max 8s), now unbounded… must fix to preserve the Ctrl-C guarantee").
4. Fix agent → created shared `backoff.py`, threaded `non_interactive` through subagents/providers, added §10 subagent regression tests.
5. Second review → caught a further latent issue (nested-subagent `non_interactive` not forwarded).

---

## 3. Answers to the original three questions

**Q: Is spec-review-per-task wasted overhead?** Yes. Across 12 tasks it found exactly one
substantive issue (a stale docstring on Task 1). On Task 12 it gave false confidence,
certifying conformance while a real unbounded-sleep regression sat underneath. Because
the plan pre-writes the exact assertions and file list, spec-review degenerates into
"re-run tests + diff filenames + check commit string" — a conformance check, not a
correctness check. ~14m30s + ~280K tokens. Fold it into code-review.

**Q: Is the reconcile/find-conflicting-tests/TDD-to-green judgment work worthwhile?**
Yes — but the real value comes from the **code-review** step, not a bigger implement
model. Reconciling pseudo-code to real signatures: all models did fine. Discovering the
un-enumerated second code path: no implementer did (incl. opus). The review→fix loop is
the engineering.

**Q: Could coding use haiku while reviewing with a stronger model?** Mechanically yes —
implement quality was model-invariant (all three produced the same scope and all missed
the same gap), so the review gate matters far more than implementer tier. But haiku is
the wrong pick on wall-clock: consistently the slowest, ~2× the tool iterations —
strictly worse on a serial pipeline, even though its per-token price is lowest.

---

## 4. Test-suite quality (~1069 tests, 13.1K src / 23.7K test LOC, 1.8:1)

Bimodal:
- **High value:** behavioral/integration tests (only 16/158 files mock — real code
  paths), the 2 `test_bug_*` repro guards, the §10 subagent regression tests the fix
  agent added. Real regression protection.
- **Low value / high tax:** ~517 exact-string-literal `==` assertions, and plan-dictated
  conformance tests born by the planner pre-writing the assertion and the implementer
  transcribing it — they confirm transcription, not intent, and can't catch a flaw the
  planner also missed. The §2 follow-up commit was literally "pin editing_mode rejection
  message." Last 40 commits: +1916 / −252 test lines — every behavior change rewrites
  tests, and "find-and-delete conflicting tests" is recurring busywork.

The Task 12 result is the proof: conformance tests + spec-review went green on a broken
feature; the tests that mattered only existed after code-review discovered the path.

---

## 5. Light-task experiment (Task 1 §1 ESC removal, Task 4 §3 spacing) — 3-way

| | sonnet | opus | haiku |
|---|---|---|---|
| Task 1 §1 | 2m33s / 22 tools | 2m08s / 22 | 4m16s / 46 |
| Task 4 §3 | 2m11s / 15 | 2m29s / 12 | 2m25s / 26 |

- Implement is model-invariant on bounded work — all three produced functionally
  identical, spec-correct, green results.
- **The un-enumerated-ripple miss repeats on a light task:** the original §1 commit also
  edited `slash.py` to delete a now-false help line ("ESC  Re-prompt without sending")
  and a docstring — not in the prompt's file list — and **all three fresh models left
  both.** Tier didn't help.
- Wall-clock compresses on small tasks except haiku still churns (46 tool calls on a
  trivial deletion). Haiku never faster; no quality upside.

---

## 6. Review experiments — OpenAI in the mix, ensemble, prompt-slicing

Goal: does adding OpenAI, running 5 reviewers in parallel (same prompt), or splitting
review into 5 bug-class slices reliably catch the seeded subagent-backoff gap (and other
bugs) — on the narrow T12 diff and the full 38-file branch diff (gap present in both).

### Method
- Unified agentic review harness (`/tmp/haiku-test/reviewer.py`): identical read-only
  `bash`-tool loop for OpenAI (chat-completions; codex via Responses API) and Anthropic.
  Reviews run in a disposable worktree at `subagents.py:209` raw `anyio.sleep`. Gap is
  detected by MY grep of each written review — never revealed to the model.
- Models: `gpt-5.5`, `gpt-5.3-codex`, `gpt-5.4-mini` (OpenAI); `opus`, `sonnet` (Claude).
- CAVEATS (honest): (1) Claude API key = 30k-input-tok/min → Claude ensembles run via the
  Agent tool, OpenAI via the harness; cross-provider scaffolding differs — compare
  within-method. (2) The OpenAI monolithic prompt kept a strengthened "grep every
  backoff/sleep loop" hint; the Claude full-diff monolithic prompt was more general, so
  cross-provider monolithic numbers are confounded by prompt strength. (3) n small per
  cell (1–5). (4) An early Claude batch was discarded after the GAP self-report
  instruction leaked the answer. (5) gpt-5.5 tripped OpenAI per-model TPM under
  parallelism; those runs were re-run serially.

### Gap-detection matrix
| Method | gpt-5.5 | opus | sonnet | gpt-5.3-codex | gpt-5.4-mini |
|---|---|---|---|---|---|
| Single (orig prompt, narrow) | ✓ | ✓ | (429) | ✗ | ✗ |
| Single (strengthened prompt, narrow) | ✓ | — | — | ✓ | ✓ |
| Ensemble 5× same (narrow) | 5/5 | 4/5 | 1/5 | 0/5 | — |
| Completeness slice (narrow) | ✓ | — | ✓ | ✓ | — |
| Other slices (narrow) | conc/term/edge ✓ | — | all ✗ | all ✗ | — |
| Monolithic (full 38-file diff) | 5/5 | — | 0/5 | 1/5 | — |
| Completeness slice (full diff) | ✓ | — | ✓ | ✓ | — |
| concurrency slice (full diff) | ✓ | — | ✓ | ✗ | — |

**The completeness slice found the gap in 6/6 (model × diff-size) cells — 100% —
including every condition where the monolithic prompt failed (codex 0/5, sonnet on the
full diff 0/5).** It is the only method robust to both weak models and large diffs.

Representative harness metrics (narrow, single): codex 6 tools/25s; mini 12/24s;
gpt-5.5 13/118s; opus 24/110s. codex is a fast, shallow, diff-only reviewer (2–6 tool
calls) that rarely greps the wider repo.

### Findings
1. **OpenAI is competitive-to-better at this gap.** `gpt-5.5` found it on every monolithic
   run (narrow 5/5 + full 5/5) — the most reliable monolithic finder of any model.
   `gpt-5.3-codex` is the opposite: shallow/diff-only, 0/5 narrow, 1/5 full.
2. **Model tier is NOT the lever; review METHOD is.** A weak finder is rescued by method,
   not a bigger model. The earlier "fresh opus & sonnet both missed it" was n=1 noise:
   opus is 4/5, sonnet ~1/5, gpt-5.5 10/10 monolithic.
3. **Ensemble (5× same) rescues PROBABILISTIC finders, not SYSTEMATIC blind spots.**
   sonnet 1/5 → union catches it; codex 0/5 → 5× of a shallow review is still 0.
4. **Slicing > ensemble; the completeness slice is the highest-value pass.** "For every
   changed symbol, grep ALL callers/parallel paths" caught the gap 100% — across every
   model and diff size — because it's a systematic procedure, immune to model weakness
   and attention-dilution.
5. **Monolithic review does NOT scale to large diffs; slices do.** On the 38-file diff
   with a general prompt the sonnet monolithic ensemble found the gap 0/5 (attention
   spread thin); completeness/concurrency slices found it anyway — same diff, same model.
6. **Prompt explicitness rivals model choice.** Adding "grep every backoff/sleep loop" to
   the monolithic prompt took cheap models miss→hit (mini, codex single). The original
   softer "grep callers of decide" hint was found by only the two flagship models.
7. **The methods surfaced TWO additional REAL bugs (verified against live code):**
   - **hint-cap inversion** (`error_policy.py:23`): `min(60, max(delay, hint))` caps our
     OWN exponential at 60s once delay>60, contradicting the docstring ("60s cap is on the
     hint only"). `decide(n=6, hint=5)` → 60.0, intended 64.0. Found by ~48 reviews across
     ALL models (it's in-diff).
   - **`2**1024` OverflowError** (`error_policy.py:21`): `1.0*(2**prior_backoff_attempts)`
     raises `OverflowError: int too large to convert to float` at ~1024 retries in
     interactive mode (no ceiling) → process crash. Confirmed live. Found by ~11 reviews,
     mostly `gpt-5.5` — a deep-tail bug.
   Visibility stratifies by effort: in-diff (hint-cap, everyone) → cross-cutting (subagent
   gap, strong models + completeness slice) → deep-tail (overflow, gpt-5.5 + more samples).

---

## 7. Bottom line / recommendations for imp

1. **Parallelize execution.** The 1-at-a-time pipeline is the dominant time sink
   (~104m serial → ~20m). Biggest win, no quality cost.
2. **Drop spec-review-per-task; fold conformance into code-review.** ~14m + 280K tokens
   reclaimed for a near-zero quality cost.
3. **Restructure code-review as a small parallel panel of bug-class slices, and ALWAYS
   include a completeness/all-call-sites slice** ("for every symbol/behavior the diff
   changes, grep the whole repo for all callers and parallel paths; verify consistency").
   That one pass is the cheapest reliable defense against the cross-cutting-gap class —
   the bug type that slips monolithic review and scales worst with diff size. It caught
   the seeded gap 100% of the time, including with cheap models.
4. **Ensemble (N× same) helps probabilistic finders but is dominated by slicing.**
5. **Stop pre-writing test assertions in the plan.** State behavioral intent; let the
   implementer/reviewer derive tests. Treat the `test_bug_*` repro pattern as the
   template. Budget exact-string pins to contracts users actually depend on.
6. **Model selection:** implement quality was model-invariant — tier the implementer for
   speed (avoid haiku: slowest; opus≈sonnet within noise), and **spend the model budget
   on the review panel**: gpt-5.5 and opus are the strong generalist finders; codex is a
   poor fit (too shallow) unless boxed into a completeness slice; sonnet is fine inside a
   slice but unreliable as a lone monolithic reviewer.

---

## Appendix — artifacts
- Worktrees/branches (disposable): `/tmp/wt-{haiku,sonnet,opus}-t{1,4}`,
  `/tmp/wt-{haiku,opus}-t1{0,2}` (gap-present narrow+full review target = `/tmp/wt-opus-t12`).
- Review harness + driver: `/tmp/haiku-test/reviewer.py`, `/tmp/haiku-test/exp.py`.
- Prompts: `/tmp/haiku-test/*_prompt.txt`. Reference diffs: `/tmp/haiku-test/sonnet_t1*.diff`.
- ~75 review outputs: `/tmp/haiku-test/results/<experiment>/<label>.md` + `_summary.json`.
