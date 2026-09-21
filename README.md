<p align="center">
  <img src="docs/images/imp-logo.svg" alt="imp terminal splash with pixel-art mascot and prompt" width="640">
</p>

# imp

**Stop babysitting your coding agent.**

<p>
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/status-pre--release-orange" alt="Status: pre-release">
</p>

imp is an AI agent workflow engine for developing software.  It takes a change
from idea through interview, spec, planning, implementation, review, testing,
integration, and merge.

It runs coding agents and LLM providers through a deterministic workflow,
avoiding long human-managed Claude Code sessions by splitting the development
process into a configurable series of short-lived, focused LLM sessions.

imp builds itself: 875 imp quests merged since May 2026.

## Why use imp?

- **Unattended after the interview** — Human effort is concentrated up front in
  a single interview; once you approve, imp drives spec, plan, code, test,
  review, and merge on its own, so you're free to step away
- **Lifecycle management** — Every change lives in a *quest*: a durable unit
  of work with a unique id, tracked from the opening idea through to the merge,
  archived on disk as a self-contained record
- **One interruption, not twenty** — When a question or spec deviation comes
  up mid-run, imp makes a judgment call and continues, deferring the final
  decision to the end, where you approve the change, send it back for rework,
  or file a follow-up as a backlog quest
- **Full provenance for every line of code** — Every conversation in a quest
  is archived, so you can trace any code to the discussion that produced it
- **Fresh context and independent reviewers** — Each step gets its own agent
  with a clean slate, with specs, plans, and progress files carrying decisions
  forward, avoiding long sessions where agents get increasingly confused
- **Bugs rooted out early** — The later a flaw is caught, the more it costs to
  fix, and an agent will faithfully implement a flawed spec, so imp loops
  on adversarial review over the spec and plan
- **Built-in regression protection** — imp leans heavily on TDD and writes a
  lot of regression tests (imp itself has over 11,000); keeping them green is
  the agents' job, not yours
- **Run multiple quests in parallel** — A quest runs in its own git worktree,
  so several can run in parallel, and can be paused or resumed at any time

## Examples

With the [default workflow](#default-workflow), a quest typically takes a
couple of hours (of imp's time, not yours) and ~3% of a weekly Claude Max 20x
quota.  See [Token costs](#token-costs) below.

Here are some recent real-world examples of imp running on its own code base.

### Adding a feature

*[Edited for brevity.  Full transcript [here](docs/examples/samo71.md).]*

```console
$ imp new "add a --json flag to imp list"
samo71 · new

  [imp splash screen]

> add a --json flag to imp list

  [3 explorer subagents map the code in parallel]

• Named this quest `JSON output for imp list`.  Here's what I found and what
  I propose.

  Today imp list gathers rows in src/imp/cli/main.py:3383 (_overview_lines)
  and :3420 (_filtered_overview_lines), each of which loads data *and*
  immediately renders it through quest_row.format_quest_line.  The underlying
  data is already clean, so adding --json is mostly a matter of splitting
  gathering from rendering, then serializing the rows instead of formatting
  them.

  The one real design question is the shape.  imp already has three --json
  paths to be consistent with: imp show --json emits one object with machine-valued
  status (merge_blocked, not "merge blocked"), and imp events search --json
  emits a flat array where each hit carries a zone of active / backlog /
  archive.

  My recommendation: a flat, zone-tagged array, in exactly the board's
  display order (in-flight first, then backlog, then history):

  [
    {
      "id": "fego87",
      "zone": "active",
      "title": "README that shows imp working",
      "status": "waiting",
      "step": {"phase": "review", "name": "spec-review"},
      "created": "2026-09-17T08:41:02+00:00",
      "last_activity": "2026-09-17T14:35:11+00:00"
    },
    [...]
  ]

  The alternative is a grouped object ({"active": [...], "backlog": [...],
  "archive": [...]}), which reads a bit more like the board but forces
  consumers to iterate three keys [...]

  Two things I'd fold in alongside: extracting the shared row-gathering out of
  _overview_lines / _filtered_overview_lines so text and JSON read from one
  source (otherwise the two formats will drift), and updating the places that
  document the command — the imp-knowledge skill's imp list bullet,
  docs/guide/introspection.md:45, and the README's command block, all of which
  currently list the flag set without --json.

  Does the flat zone-tagged array look right to you?

> ok

• Settled, then. To read it back in one piece before I set it up:

  Goal — imp list gains a --json flag that emits the board as structured data
  for agents and scripts.

  Scope — the flag on the list parser; a JSON branch in _cmd_list; extraction
  of the shared row-gathering so both formats read one source; updates to the
  imp-knowledge skill, docs/guide/introspection.md, and the README.

  Decisions — flat array in board order, zone-tagged active / backlog /
  archive; raw ISO timestamps rather than relative ages; title null when
  unnamed, with the full opening prompt in intent; step present only on
  in-flight entries that have one; machine status values kept underscored;
  all existing filters compose unchanged; no matches emits [].

  Acceptance — the output parses, carries the right entry per zone with the
  fields above, stays free of ANSI escapes, composes with --all / --backlog /
  --since, returns [] with exit 0 on an empty board, and leaves today's text
  output byte-identical.

  Ready for me to set this up and start building? Declining sets it aside, and
  you can pick it back up later with imp resume samo71.

> ok

• I'll fork the worktree and write the spec.

  [spec]                       ✓  (2 rounds of review)
  [plans 1-3]                  ✓  (4 rounds of review)
  [implementation]             ✓  (each commit reviewed individually)
  [full test suite]            ✓
  [final code review]          ✓
  [integration]                ✓

Merged samo71 into master.
```
Mid-run screenshot:

<p align="center">
  <img src="docs/images/hero-run.png" alt="imp mid-run: the live status bar shows the quest, phase, plan and task counters, model, and context gauge, above a coder subagent's own gauge">
</p>

<p align="center">
  <em>Mid-flight: plan 1 of 3, task 1 of 3, with a <code>coder</code> subagent working under a context budget.</em>
</p>

Stats:
- 2h 6m runtime
- human input: 1 opening prompt, 2 approvals
- 10 commits, 9 files, +641/-55
- 26 new tests
- 2.8% of a weekly Claude Max 20x quota (~0.7% of monthly total)

It produced the following logically sequenced, independently reviewable
commits:

```console
$ git log --oneline --reverse --no-merges --grep='Quest: samo71'
51a98d03 cli: share one history-selection expression
9ca92310 cli: extract the overview row gatherer
390bf01c cli: load the unfiltered board through the gatherer
7671137b cli: encode board rows as JSON entries
e449e01d cli: add imp list --json
d6102f9a cli: emit [] from imp list's no-control-root exit
944a7e72 skills: name imp list --json in the knowledge cheat-sheet
cb997ad5 docs: document imp list --json in the guide and README
e54d5b3a cli: drop the dead _history_rows shim
20b0569f tests: wrap the retargeted history-disposition calls
```

Note that only **three of the ten commits implement the actual feature**:

```console
7671137b cli: encode board rows as JSON entries
e449e01d cli: add imp list --json
d6102f9a cli: emit [] from imp list's no-control-root exit
```

Five commits are **refactorings and cleanups**:

```console
51a98d03 cli: share one history-selection expression
9ca92310 cli: extract the overview row gatherer
390bf01c cli: load the unfiltered board through the gatherer
e54d5b3a cli: drop the dead _history_rows shim
20b0569f tests: wrap the retargeted history-disposition calls
```

And the remaining two are **proactive documentation updates**:

```console
944a7e72 skills: name imp list --json in the knowledge cheat-sheet
cb997ad5 docs: document imp list --json in the guide and README
```

Full quest log and quota summary: [`docs/examples/samo71.md`](docs/examples/samo71.md)

### Fixing a bug

*[Edited for brevity.  Full transcript [here](docs/examples/haje70.md).]*

```console
$ imp new "why did kelu74 fail"
haje70 · new

> why did kelu74 fail

  [4 explorer subagents read kelu74's event log and map the provider stack]

• I've named this `Fix codex turn-boundary teardown race`.  Here's the design.

  ## Root cause, confirmed

  A prior quest (nehe62, merged 2026-07-21, "Fix pending-task warning from
  abandoned provider generators") established the discipline for this exact
  hazard: every place that iterates a provider stream wraps it in
  contextlib.aclosing, so teardown cascades synchronously instead of being
  abandoned to garbage collection.  It wrapped the sites that existed then.

  src/imp/agents/agent_loop.py was created the *next day*, and the codex
  session was later folded onto it.  Its `async for item in session.events():`
  at line 41 is the one iteration site that never got the wrap — and it's the
  only caller of session.events() in the tree.  Its `break` at line 62 abandons
  the whole chain mid-yield, so the codex process's teardown is deferred to a
  GC-scheduled callback.  When the next turn spawns before that lands,
  publish() finds the slot still occupied and the bare assert fires.

  [proposes the fix, plus a traceback detail so the next unclassified
   provider error is diagnosable from the event log alone]

> ok

  [spec]                       ✓  (4 rounds of review)
  [plans 1-2]                  ✓  (5 rounds of review)
  [implementation]             ✓
  [full test suite]            ✓
  [final code review]          ✓
  [integration]                ✓

Merged haje70 into master.
```

Stats:
- 2h 12m runtime
- human input: 1 opening prompt, 1 approval
- 4 commits, 9 files, +266/-91
- 5 new regression tests
- 3.0% of a weekly Claude Max 20x quota

It debugged the issue and then (after getting the user's approval) produced the
following logically sequenced, independently reviewable commits:

```console
$ git log --oneline --reverse --no-merges --grep='Quest: haje70'
dde6ebcf agent_loop: close the provider event stream at the agent-loop turn boundary
73595da4 observe_session: close _receive on every exit path from events()
d04c527e fix: make an unclassified provider error legible in the event log
114fea1e loop_items: tighten Session.events() return type to AsyncGenerator
```

It fixed the race in the first two commits:

```console
dde6ebcf agent_loop: close the provider event stream at the agent-loop turn boundary
73595da4 observe_session: close _receive on every exit path from events()
```

Then it **proactively improved error reporting output** for future provider
errors by adding a traceback and crash site to the event log:

```console
d04c527e fix: make an unclassified provider error legible in the event log
```

Full quest log and quota summary: [`docs/examples/haje70.md`](docs/examples/haje70.md)

## Token costs

Both of the above [examples](#examples) took 2+ hours of clock time.  The
[default workflow](#default-workflow) can be quite a bit more heavyweight than
a typical Claude session.  Most of the time and tokens go towards design and
planning (through multiple rounds of review), before any code is written.  That
up-front effort makes the code less buggy, more maintainable, and more likely
to meet the user's expectations, with fewer "surprises".

Quality isn't cheap.  When using the default workflow, **a Claude Max (or
ChatGPT Pro) subscription is recommended.**  Subscriptions are *much* cheaper
per token than API billing.  With Anthropic API billing, the `--json` feature
above would have cost ~$44, and the bug fix (including debugging) would have
cost ~$49.

The example runs each cost about ~3% of my weekly Claude Max 20x window.  That
means *roughly* 30 quests per week for a single subscription.

Multiple quests can run in parallel on the same project.  I often start 3-5
quests at the same time, batching my "human input", and then just let them run
unattended for a few hours (or overnight).

If you want to use fewer resources for a given project or quest, you can create
your own workflow YAML file.  IMO, the overhead of the default workflow is well
worth the cost if you want a maintainable project with decent code.

Local models are not (yet) supported.  Pull requests welcome :-)

## Guardrails

- **Stuck/runaway agent detection** — imp pauses the run when it detects
  excessive token usage, review loops, or agent-reported errors
- **Flaky test handling** — Any test failures unrelated to the quest are noted
  and reported to you at the merge gate, where they can be deferred to a
  backlog quest or ignored
- **Transparency** — All agent chat logs (including thinking) are logged to
  disk and can be audited
- **Notifications** — imp can ping you on [Telegram](docs/guide/remote-control.md)
  if your attention is needed, where you can reply directly
- **Auto merge** — If there are no open issues for you to decide on, it
  auto-merges to your integration branch (this default is configurable)
- **Local git only** — imp doesn't push to or fetch from any git remotes; you
  are free to push the quest-specific branch, or create your own PR on GitHub
  (GitHub integration is planned)

## Try it

imp needs Git, Python 3.11+, and `uv`.

For automatic sandboxing (currently Linux-only), it also needs
[bubblewrap](https://github.com/containers/bubblewrap), and util-linux 2.38+
(`nsenter`, `unshare`).

Login to your `claude` CLI (or `codex`) and ensure they're in your path.  Or to
use provider APIs, export `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.  imp has a
pluggable provider backend but local models are not yet supported.

### Install

```bash
uv tool install git+https://github.com/jpoimboe/imp
```

Or install from a local clone:

```bash
git clone https://github.com/jpoimboe/imp && cd imp
uv tool install .
```

### New project (greenfield)

```bash
mkdir /path/to/my-new-app
cd /path/to/my-new-app

# imp init does the following:
#  - create imp/config.yaml and imp/memory scaffolds
#  - add .imp/ to .gitignore
#  - git init (if needed) && git commit
#  - bootstrap ~/.imp/config.yaml (providers, theme)
imp init

# create the initial quest and start the interview
# (quest state is persisted in ~/.imp/projects/my-new-app)
imp new "build a local-first task tracker with a web UI"
```

After creating and initializing the repository with `imp init`, `imp new`
starts the interview.  It asks questions, proposes options, pushes on
tradeoffs, searches the web, and does experiments as needed.

When the design is settled, it creates a git worktree and starts the autonomous
run.  You can pause it at any time with Ctrl-D for resuming later with `imp
resume`, or interrupt at any time with Ctrl-C to send an agent in a different
direction.

### Existing project (brownfield)

Brownfield projects are also supported.  Just run `imp init` and `imp new` from
an existing git checkout.

```bash
cd /path/to/my-existing-app

# imp init does the following:
#  - create imp/config.yaml and imp/memory scaffolds
#  - add .imp/ to .gitignore
#  - git init (if needed) && git commit
#  - bootstrap ~/.imp/config.yaml (providers, theme)
imp init

# create the initial quest and start the interview
# (quest state is persisted in ~/.imp/projects/my-existing-app)
imp new "add rate limiting to the public API"
```

### Disabling auto-merge

To approve every merge by hand, put the following in `imp/config.yaml`:

```yaml
vars:
  auto_merge: false
```

imp will then stop and ask before merging.  You can then approve, send it back
for rework, open related backlog quests for followups, or pause it
indefinitely.  Pass `--var auto_merge=false` to `imp new` to do so for a single
quest.

### Chat

If you want to ask imp questions about your code base, about another quest, or
about imp itself, you can run `imp chat` rather than `imp new`.  If the
conversation leads to the need for a code change, it can convert itself into a
proper quest.

## Quests

A *quest* represents a unit of work, from the opening intent to the final
merge: kind of like a pull request that starts at the idea instead of the
finished diff.

After the initial interview, the quest "goes live", with its own worktree,
artifacts, and event log.  Prompts, responses, and tool outputs are logged
alongside decisions, test runs, and gate outcomes.  State is rebuilt from the
log rather than held only in memory, so an interrupted run can be resumed in
the middle of a step.

When the quest finishes, its conversation and artifacts are archived and remain
searchable.  Quests can be interacted with via the CLI:

```bash
imp list                         # active work, drafts, and backlog
imp show <id>                    # intent, status, timing, and result
imp log <id>                     # readable story of the run
imp log --summary <id>           # per-step ledger of time, tokens, and quota
imp search "rate limit"          # live and archived conversations
```

Every commit log produced by the default workflow carries a `Quest: <id>`
trailer.  That makes it possible to associate every line of code with the
quest and conversation that created it:

```bash
git blame path/to/file
git log -1 --format='%(trailers:key=Quest,valueonly)' <commit>
imp log <quest-id>
```

You can also see all in-progress, backlog, and completed/archived quests:

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

And full fast search is supported:

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
```

## Default workflow

imp runs workflows, each of which consists of a YAML file and agent markdown
files.  The default "batteries included" workflow can be found [here](src/imp/assets/workflows/default/workflow.yaml).

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

## Isolation and sandboxing

Each live quest runs in a separate git worktree, which enables parallel runs,
but is not a proper isolation boundary.

To achieve that, imp runs agent shell commands under bubblewrap, in separate
user, IPC, and UTS namespaces.  The filesystem is read-only outside an explicit
writable set; credentials are replaced with empty mounts; and shared Git escape
paths such as hooks, config, the index, and refs outside the quest's namespace
are remounted read-only.

Network access is allowed by default; set `sandbox.network: false` to disable it.
File-editing tools enforce the same path policy in-process, so the kernel-level
guarantee applies to shell commands.

## Status and contributing

imp is pre-release.

See [Architecture](docs/internals/architecture.md) for an overview of the engine
and bundled workflows.

Issues and pull requests are welcome.  To work on imp itself, clone the repo and
run `make sync-env` for a `.venv` matching `uv.lock`, then `make check` before
opening a pull request.
