<p align="center">
  <img src="docs/images/imp-logo.svg" alt="imp terminal splash with pixel-art mascot and prompt" width="640">
</p>

# imp

**Stop babysitting your coding agent.**

![license](https://img.shields.io/badge/license-MIT-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![status](https://img.shields.io/badge/status-pre--release-orange)

**imp is a declarative agent workflow engine for AI software development.**
Each change (feature or bug) for a given project gets built as part of an
isolated, resumable **quest**, which tracks the change's history from inception
through design, planning, coding, review, testing, and integration.

Every conversation is archived, so you can easily **trace any line of code back
to the full conversation which produced it.**

**imp is workflow-agnostic, but ships batteries included** with a default
opinionated workflow, which follows AI software engineering best practices for
producing AI written, reviewed, and tested code in a fast-moving greenfield or
small team project.  The default workflow is defined in YAML and can be easily
customized or replaced with a completely different workflow.

**Human effort is concentrated up front** at the beginning of the quest, during
an interview with the "threshold" agent.  Once it has picked your brain
sufficiently, with your approval it runs unattended from spec, plan, code,
test, review, all the way through merge, so you're free to step away from the
computer.  The quest can be paused or resumed at any time.  You can also kick
off another quest for the same project in parallel, as imp runs in **resumable,
isolated git worktrees.**

For any surprises or open items discovered during the quest, imp agents make
judgment calls which are then raised back to the user at the merge gate for
their final approval.  They can then send it back for rework, or open a
follow-up backlog quest for further improvements.

imp uses **fresh LLM context and independent reviewers** so each agent can
focus on its given task, avoiding long sessions where agents get increasingly
confused.  Detailed specs, implementation plans, and progress files carry
decisions between steps.  Tests are written before code (TDD), every artifact
is independently reviewed in multiple rounds if needed, and the final code is
checked against acceptance criteria before merge.

**Bugs are rooted out early.**  Generally with AI development, most bugs tend
to originate early during the spec or planning phase, due to gaps,
inconsistencies, or mistakes made by the agent.  imp prevents that by spending
most of its tokens up front, looping on multiple rounds of adversarial
spec/plan review until everything is fully specified and verified.

imp's default workflow produces **clean and maintainable code.**  The agents
look for cleanups, refactorings, simplifications, and code removals. 

It uses **test-driven development (TDD)** extensively.  TDD tends to produces a
*lot* of tests that might be considered fragile by a human, as even trivial
changes can trigger test failures.  But LLMs have no problem dealing with that,
so TDD seems to be a good fit for preventing regressions with AI-native
development.

imp is currently Linux-only.  Support for macOS is planned.

## Example

<p align="center">
  <img src="docs/images/hero-run.png" alt="imp mid-run: the live status bar shows the quest, phase, plan and task counters, model, and context gauge, above a coder subagent's own gauge">
</p>

<p align="center">
  <em>Mid-flight: plan 1 of 3, task 1 of 3, with a <code>coder</code> subagent working under a context budget.</em>
</p>

Note the default workflow can be a bit token-intensive due to the extra
thinking involved with detailed specs, plans, and multiple rounds of
independent review.  Quality, maintainable, functional, well-tested code isn't
cheap.  **Claude Max (or ChatGPT Pro) subscription recommended for the default
workflow.**  Local models are not (yet) supported.  Pull requests welcome :-)

For a lighter weight approach, custom workflows can be created, while still
taking advantage of imp's useful [engine features](#engine-features).

Here's an example run, adding a --json flag to `imp list`:

<!-- docs-assets:excerpt -->
```console
$ imp new
samo71 · new

  … imp splash screen …

> add a --json flag to imp list

  … 279 events elided (brief human interview) …

• Settled, then. To read it back in one piece before I set it up:

  Goal — imp list gains a --json flag that emits the board as structured data
  for agents and scripts.

  Scope — the flag on the list parser in src/imp/cli/main.py; a JSON branch in
  _cmd_list; extraction of the shared row-gathering out of _overview_lines /
  _filtered_overview_lines so both formats read one source; updates to the
  imp-knowledge skill bullet, docs/guide/introspection.md, and the README
  command block.

  Decisions — flat array in board order, each entry zone-tagged active / backlog
  / archive; raw ISO timestamps rather than relative ages; title null when
  unnamed with the full opening prompt in intent; backlog entries carry status:
  "backlog" plus provenance and readiness; step present only on in-flight
  entries that have one; machine status values kept underscored; all existing
  filters compose unchanged; no matches emits [].

  Acceptance — the output parses, carries the right entry per zone with the
  fields above, stays free of ANSI escapes, composes with --all / --backlog /
  --since, returns [] with exit 0 on an empty board, and leaves today's text
  output byte-identical.

  Ready for me to set this up and start building? Declining sets it aside, and
  you can pick it back up later with imp resume samo71.

> ok

  … 2,397 events elided (spec+review, plan+review, implementation+review, testing) …

• I walked all twelve acceptance criteria clause by clause against the committed
  code across the eight commits. Coverage is complete — every clause lands on a
  production line, and the behavioral ones have pinning tests.

  The three outline plans each delivered their scope:

   • The gatherer extraction is real: _overview_rows at src/imp/cli/main.py:3451
     loads and zone-sorts rows keyed on dispositions + cutoff, with
     _overview_lines (3416) and _filtered_overview_lines (3475) reduced to
     rendering wrappers that kept their signatures. The five test files the spec
     named as direct callers are untouched by the diff, and _cmd_status is
     unmodified — so AC 10(a) and 10(b) hold.
   • The flag sits at src/imp/cli/main.py:4414, after --since and before
     --no-pager with the help text verbatim (AC 11). All three _cmd_list exits
     honor it: the PreconditionError branch at 2091, the unfiltered branch at
     2101 (after disposition resolution), and the filtered branch at 2118 —
     which is reached after the --since parse, so an invalid spec still exits 2
     with a clean stdout (AC 8).
   • The encoder at 3487–3522 emits exactly the seven always-present keys, gates
     step on r[2] not in ("", "-") mirroring _quest_json's gate at 2056, reads
     provenance from slot 1 and derives readiness from slot 2 without re-calling
     backlog_readiness, and forces intent=None for backlog (AC 2–4). status
     flows straight from the machine word — the merge blocked spacing only ever
     happens in quest_row, which the JSON branch never reaches, and
     src/imp/cli/store.py:470 confirms the row slot stays underscored (AC 5).

  A couple of clauses worth naming explicitly. AC 2(a)'s "untruncated" is
  structurally guaranteed: intents comes from HomeDigest.intent_title, which is
  raw _resolve_intent output (src/imp/cli/store.py:151), and
  tests/test_cli_list_json.py:170 pins it with a 92-character two-line prompt
  that the board's 70-column cap would have clipped and collapsed. AC 9's
  ANSI-freedom is pinned twice — once with color forced on, once with isatty
  forced true. The one edit to an existing test
  (tests/test_cli_trailing_blank.py) only adds json=False to a hand-built
  Namespace; its asserted output is byte-identical.

  Documentation carries the flag on all three surfaces with the verbatim text,
  and the two assertions went into the right tests — the skill ordering check
  reads the loaded skill body, the guide check reads docs/guide/introspection.md
  — so the confusion the spec warned about didn't happen.

  Both deferred.md entries are closed in code, not just marked:
  _board_entry_json strips the title and gates intent on that same stripped
  value (pinned by the title="  " case at tests/test_cli_list_json.py:119), and
  the emission path has its piped/--no-pager test at 319.

  … 459 events elided …

• The base branch hasn't moved since the fork point, and there are no open
  decision items, so this one merges automatically without a gate.

  What it does. imp list gains a --json flag that emits the quest board as a
  structured JSON array — one object per quest, in display order (in-flight →
  backlog → archive) — so agents and scripts can read board state without
  parsing the padded, colored, width-capped text table. The implementation first
  extracts a shared row-gathering function (_overview_rows, keyed on
  dispositions and cutoff) so the text and JSON paths draw from one source and
  cannot drift in selection or ordering, then layers the encoder and flag on
  top. Every existing filter composes with it; an empty result always emits []
  with exit 0 (including the path where no control root exists); a bad --since
  spec still exits 2 as a usage error. The JSON shape reuses the vocabulary imp
  show --json and imp events search --json already established — id, zone,
  title, intent, status, created, last_activity, plus step on active entries and
  provenance/readiness on backlog ones. The flag is documented in the
  imp-knowledge skill, the introspection guide, and the README, each surface
  pinned by the test that reads it.

  Verification. The combined checks pass. There was one round-trip on the way
  here: a line-length lint failure in tests/test_overview_dedup.py, introduced
  by the cleanup that deleted a dead _history_rows shim and retargeted its test
  callers onto the longer _history_by_disposition name. That was fixed forward
  in fb9c9656 and the suite came back green.

  Open items. None. All three recorded followups were resolved during the run
  rather than deferred — two spec-conformance items folded into their tasks, and
  the dead-shim deletion the diff review caught, fixed in 9e1bb977.

  Approving this now; the merge runs immediately. Total diff is 9 files,
  +641/−55, most of it new tests.

  1 call · complete_step

── integration phase ───

integration phase · merging · complete

Merged samo71 into master.
```
<!-- /docs-assets:excerpt -->

Full summary:

- 3 human prompts ("add a --json flag to imp list", "ok, "ok) during the
  interview phase
- full generated specification file (with 2 rounds of independent review)
- 3 implementation plans (each with 1-2 rounds of independent review)
- 10 code commits (reviewed)
- 26 new tests
- full diff code review
- full test suite pass
- 0 open items at the merge gate
- ~$1.00 (0.5%) of monthly total Claude Max $200 subscription quota (or $42.40
  of theoretical API spend)

<!-- docs-assets:ledger -->
```console
$ imp log samo71 --summary
default  ·  samo71  ·  merged
                                         time      in     out      cost
threshold
  threshold                            5m 20s    1.4M   27.8k   [$2.45]
  … 4 rows elided …
spec
  spec-finalizer                        2m 8s  278.0k   10.5k   [$0.79]
  spec-verify-commit                       0s       —       —         —
  spec-reviewer                        4m 51s  648.3k   22.6k   [$1.44]
  spec-finalizer                       1m 49s  625.2k    8.7k   [$1.00]
  spec-verify-commit                       0s       —       —         —
  spec-reviewer                        4m 44s  748.3k   22.3k   [$1.48]
plan
  plan-writer                          8m 35s    1.5M   42.4k   [$2.84]
  plan-verify-commit                       0s       —       —         —
  plan-reviewer                        6m 15s    1.5M   29.0k   [$2.24]
  plan-finalized                           0s       —       —         —
implement
  task-lead                           11m 27s    2.0M   48.6k   [$2.99]
    ↳ coder (writing code)                47s  149.6k    2.9k   [$0.19]
    ↳ code-reviewer (reviewing code)    1m 6s   45.7k    4.2k   [$0.13]
    ↳ coder (writing code)             1m 25s  265.0k    6.1k   [$0.29]
    ↳ code-reviewer (reviewing code)   1m 29s   72.5k    5.8k   [$0.20]
    ↳ coder (writing code)              1m 3s  163.9k    3.0k   [$0.19]
    ↳ code-reviewer (reviewing code)   1m 36s  124.8k    6.3k   [$0.23]
  verify                                2m 9s       —       —         —
  check-next-plan                          0s       —       —         —
  … 33 rows elided …
converge
  gap-check                             3m 1s  640.0k   10.6k   [$1.10]
  quest-review                         5m 30s    3.0M   21.4k   [$3.04]
  deferred-check                           0s       —       —         —
  deferred-sweep                       3m 17s  528.7k   12.0k   [$1.01]
  … 5 rows elided …
  converge-verify                       3m 7s       —       —         —
integrate
  wait-for-head                            0s       —       —         —
  integrate-rebase                         0s       —       —         —
  summarizer                              22s   52.5k    1.0k   [$0.12]
  finalize                                53s  175.6k    2.8k   [$0.42]
  cleanup-merge                            0s       —       —         —
  assert-stamped                           0s       —       —         —
  merge-to-base                            0s       —       —         —

  total                                 2h 6m   28.0M  531.1k  [$42.20]
```
<!-- /docs-assets:ledger -->

Full run: [`docs/examples/samo71.md`](docs/examples/samo71.md).

## A quest leaves a trail

A quest represents a unit of work, from the opening intent to the final merge.
Before going live, it's just a conversation: no branch or worktree.  You can
build it, park it in the backlog, or discard it.

Once live, the quest owns its worktree, artifacts, workflow snapshot, and
append-only event log.  Prompts, responses, and the tool events exposed to imp
go into that local log alongside decisions, test runs, and gate outcomes.
State is rebuilt from the log rather than held only in memory, so an
interrupted run can resume in the middle of a step.  The log can contain source
code and command output; treat the archive as source-sensitive data.

When the quest finishes, its conversation and artifacts are archived and remain
searchable.  The commit is not the only surviving explanation of the change:

```bash
imp list                         # active work, drafts, and backlog
imp list --json                  # the same board, structured
imp show <id>                    # intent, status, timing, and result
imp log <id>                     # readable story of the run
imp events <id> --agent coder    # the coder's event stream
imp search "rate limit"          # live and archived conversations
imp events search "pytest"       # tool calls, results, and agent reasoning
```

Every commit log produced by the default workflow carries a `Quest: <id>`
trailer.  That makes it possible to associate every line of code with the
conversation and full agent logs that created it:

```bash
git blame path/to/file
git log -1 --format='%(trailers:key=Quest,valueonly)' <commit>
imp log <quest-id>
```

The board, across every quest in the project:

```console
$ imp list
ID      NAME                                                   STATUS    UPDATED
fego87  README that shows imp working                          running   0s

backlog:
jara67  Revisit migrating codex to the official app-server S…  backlog   10d
timo83  Make test_cmd_slow false skip the slow gate            backlog   36d
jiro48  Restore archived-row titles and intents under the sa…  backlog   36d
  … 15 rows elided …

completed:
samo71  JSON output for imp list                               merged    2h
gepa93  Wire the completion nudge into codex live tests        merged    10h
sobi11  Adopt filelock 4.0                                     merged    10h
vuka12  Cut redundant review ceremony                          merged    10h
  … 867 rows elided …
```

And every conversation ever had about this code:

```console
$ imp search "merge queue"
433 matches for "merge queue"

vuka12 · Cut redundant review ceremony · archive · 10h
  … "wait-for-head": "waiting in merge queue", "reconciler": "resol …
  … body). _IR_SIG = "HEAD of the merge queue" def _prompt_carries_ir(**kw …

fesa14 · Wait for dependencies outside the provider call · archive · 6d
  … ** for the next quest in the **merge queue**, i.e. the next quest waiting …
  … acquire step, not yet head in merge queue → `raise AwaitingUserInput()` …
  … thing that happens when in the merge queue (and what happens today for cr …
  … +3 more

gevu23 · Resume without replaying the integrate phase · archive · 10d
  … aml # ── integrate: the FIFO merge queue (base lock from wait-for-head) …
  … lane label: waiting in merge queue rules: - {el …
  … y:1290–1293`), enqueues in the merge queue, waits until head, then **mark …
  … +8 more

  … 430 quests elided …
```

## Try it

imp needs Linux, Git, Python 3.11 or newer, `uv`,
[bubblewrap](https://github.com/containers/bubblewrap), and util-linux 2.38 or
newer (`nsenter`, `unshare`).

For model access, install and sign in to the `claude` or `codex` CLI in your
path.  To use provider APIs, export `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

```bash
uv tool install git+https://github.com/jpoimboe/imp

mkdir /path/to/my-app
cd /path/to/my-app
imp init
imp new "build a local-first task tracker with a web UI"
```

After initializing the repository with `imp init`, `imp new` starts the
interview.  It asks questions, proposes options, pushes on tradeoffs, and
spikes throwaway code when the answer needs an experiment rather than an
opinion.

When the design is settled, it creates a git worktree and runs off on its own.
You can pause it at any time with Ctrl-D for resuming later with `imp resume`,
or interrupt the agent at any time with Ctrl-C to ask a question or send it in
a different direction.

After the first quest, once the master branch has code, multiple quests can be
run in parallel in different terminal tabs.

Instead of `imp new`, you can use `imp chat` to ask imp questions about itself,
the code base, bugs or new features.

Brownfield projects are also supported.  Just `imp init` and `imp new` from an
existing checkout.

## Features

### Default workflow

```mermaid
flowchart LR
  intent([intent]) --> interview[interview]
  interview --> spec[spec]
  spec --> plan[plan]
  plan --> implement[implement]
  implement --> converge[converge]
  converge --> integrate[integrate]
  integrate --> merged([merged])
  spec -. review .-> spec
  plan -. review .-> plan
  implement -. test or review failure .-> implement
  implement -. next plan .-> plan
  converge -. gap found .-> plan
  integrate -. rework .-> plan
```

imp's "batteries included" default workflow has the following features:

- **Put human judgment up front.** imp asks about decisions it cannot make from
  the code, while changing the design is still cheap.  Once the design is
  settled, it typically drives the rest of the quest through merge on its own.
- **Start every step fresh.** Each step is a separate model invocation with only
  the context it needs, to avoid entering the LLM "dumb zone".  Specs, plans,
  and progress files carry decisions forward.  Reviewers do not inherit
  authors' reasoning or assumptions.
- **Spec-driven development.** A spec defines the result and its acceptance
  criteria.  An implementation plan names the code and tests needed to get
  there.  Both are reviewed (often multiple times) before implementation
  starts.
- **Adversarial review.** The agents reviewing the spec, plan, and code
  did not write them.  They inspect the repository and verify claims instead of
  inheriting the previous agent's assumptions.
- **Work through big changes one plan at a time.** imp implements and fast-tests
  each plan before starting the next, then reviews the whole quest as one change.
- **Test-driven development.** Tests are written early and often, and must be
  observed failing for the expected reason before implementation.
- **Keep the code lean and maintainable.** Coders and reviewers push back on
  speculative fallbacks, dead error handling, and compatibility shims that the
  design did not ask for.
- **Sweep up non-blocking review comments.** Correctness blockers loop back for
  an immediate, re-reviewed fix.  Everything else worth considering goes through
  a separate, once-per-quest quality sweep, which fixes low-risk improvements,
  filters out noise, and saves genuine judgment calls for the merge gate.
- **Save newly discovered human judgment calls for the end.** After the initial
  interview, any discovered issues are fixed automatically, with judgment calls
  deferred until the final merge gate for the user to approve, send back for
  rework, or open a backlog quest.
- **Converge on the spec.** After implementation, imp checks the whole change
  against the acceptance criteria, runs the slow tests, and routes gaps back into
  planning or implementation.
- **Triage flaky tests.** Failures caused by the change are fixed and retested.
  Flaky or environmental test failures are recorded and surfaced at the merge
  gate where they can be tracked in a new backlog quest.
- **Integrate and merge.** imp gets in a merge queue to coordinate with other
  imp quest merges, rebases onto the current base, resolves conflicts, reruns
  verification, discusses any open items with the human, and merges.

### Engine features

The workflow sits on top of engine features that keep quests isolated,
resumable, and inspectable:

- **Isolate each quest.** Once live, every quest gets its own git worktree and
  sandboxed agent tools, so several can run at once without breaking your
  system or touching your checkout, index, or stash.
- **Pause, resume, or redirect.** Pause a quest at any time with Ctrl-D, or
  interrupt a running agent with Ctrl-C to ask a question or send it in a
  different direction.  The append-only event log lets the run resume without
  throwing away the in-flight step or pretending it never happened.
- **Get notified and steer from Telegram.** imp can alert your phone when a quest
  needs input, pauses, fails, or finishes.  You can also directly interact with
  quests and create new ones from Telegram.
  See [remote-control setup](docs/guide/remote-control.md).
- **Keep the whole history searchable.** Conversations, artifacts, and tool
  events remain searchable after a quest finishes, and every line of code can
  be traced back to the conversation that produced it.
- **Inspect everything from the CLI.** See active, draft, backlog, and archived
  quests, then drill into any quest's current state, decisions, summary, readable
  log, or event stream.
- **Intra- and Inter-quest interaction.** Agents can inspect earlier steps and
  search or read other live and archived sibling quests.  With your approval,
  they can set dependencies or open new quests for work they discover.
- **Non-committal chat.** Use `imp chat` to ask questions about imp or your
  codebase, explore an idea, or scout existing work.  It remains just a
  conversation -- with no branch or worktree -- unless you decide to build,
  when it continues into the normal workflow.
- **Keep a real backlog.** Park ideas for later, or let agents file followup
  work they discover while building something else.
- **LLM provider agnostic.** imp supplies its own system prompt and agent
  tools, and disables Claude's and Codex's native equivalents.  Every backend
  gets the same instructions, tool interface, and permission checks.
- **Per-role LLM vendors/models.** A project or quest can use different model
  tiers or vendors for planning, coding, and review.
- **Carry project memory forward.** Standing, human-approved instructions in
  `imp/memory/` carry architectural decisions, local conventions, and
  cross-quest preferences into future quests.  See
  [project memory](docs/guide/memory.md).
- **Custom workflows.** Workflows, gates, model tiers, scripts, and agent
  prompts are ordinary files in your repo.  Customize the default workflow or
  replace it entirely.
- **Embed it in your own application.** A supported Python API can create quests,
  stream events, and answer the questions a workflow asks.  See the
  [embedding guide](docs/guide/embedding.md).
- **Not just for code.** The engine can drive other structured processes, not
  just software development.

## Isolation and security boundaries

Each live quest uses a separate git worktree.  Nothing touches your checkout,
index, or stash, and several quests can run at once.

A worktree is not a security boundary because worktrees share one `.git`.  imp
therefore runs agent shell commands under bubblewrap, in separate user, IPC, and
UTS namespaces.  The filesystem is read-only outside an explicit writable set;
credentials are replaced with empty mounts; and shared Git escape paths such as
hooks, config, the index, and refs outside the quest's namespace are remounted
read-only.

Network access is allowed by default; set `sandbox.network: false` to disable it.
File-editing tools enforce the same path policy in-process, so the kernel-level
guarantee applies to shell commands.

## Status and contributing

imp is pre-release.  Go forth and test.

See [Architecture](docs/internals/architecture.md) for an overview of the engine
and bundled workflows.

Issues and pull requests are welcome.  To work on imp itself, clone the repo and
run `make sync-env` for a `.venv` matching `uv.lock`, then `make check` before
opening a pull request.
