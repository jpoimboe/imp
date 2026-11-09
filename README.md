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

imp builds itself: 1,000+ imp quests merged since May 2026.

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
  lot of regression tests (imp itself has over 15,000); keeping them green is
  the agents' job, not yours
- **Run multiple quests in parallel** — A quest runs in its own git worktree,
  so several can run in parallel, and can be paused or resumed at any time

## Examples

With the [default workflow](#default-workflow), a quest typically takes under
an hour (of imp's time, not yours) and ~1% of a weekly Claude Ultra quota.
See [Token costs](#token-costs) below.

Here are some recent real-world examples of imp running on its own code base.

### Adding a feature

*[Edited for brevity.  Full transcript [here](docs/examples/seru99.md).]*

```console
$ imp new "add a --json flag to imp list"
seru99 · new

  [imp splash screen]

> add a --json flag to imp list

  [2 explorer subagents map imp list and the existing --json outputs in parallel]

• I'm calling this JSON output for imp list. Here's how the command works today:

   • imp list builds its rows itself (_cmd_list, src/imp/cli/main.py:2086) and
     prints three sections: in-flight, backlog, and history.
   • The filter flags (--active, --merged, --since, --all, and so on) all go
     through the same row builders.
   • imp show --json and imp events --json both use plain json.dumps(obj,
     indent=2), sent through the pager with no trailing blank line.
   • imp events search --json already labels each hit with a zone, using active
     / backlog / archive.

  The first decision is the overall shape. My recommendation is a flat JSON
  array, one object per quest, each tagged with a zone. It keeps the same order
  as the text board, and the filters only change which rows appear:

  [
    {"id": "seru99", "title": "JSON output for imp list", "zone": "active",
     "status": "running", "step": {"phase": "build", "name": "implement"},
     "created": "2026-10-31T09:12:03+00:00", [...]},
    {"id": "k3m9qa", "title": "Telegram retry", "zone": "backlog",
     "status": "backlog", "readiness": "blocked", "step": null, ...},
    [...]
  ]

  The alternative is an object keyed by section, like {"active": [...],
  "backlog": [...], "history": [...]}. That matches the on-screen layout more
  closely. The cost is that scripts have to join the sections back together to
  filter by status or date, and a filter like --merged would leave empty keys
  behind. I'll get to the exact field list once the shape is settled.

  Should we go with the flat array tagged by zone?

> ok

• The flat array it is. Next is the exact set of fields on each object. I'd keep
  each row to what imp list already knows without opening every quest's event
  log. [...]

   key            type                  value
   ────────────────────────────────────────────────────────────────────────────
   id             str                   quest id
   title          str | null            the title, or null when blank
   zone           str                   active, backlog, or archive
   status         str                   the status word with no display
                                        formatting [...]
   step           {phase, name} | null  only for in-flight quests that have a
                                        step; phase may itself be null
   readiness      str | null            ready or blocked for backlog items,
                                        null for everything else
   created        ISO str | null        creation time
   last_activity  ISO str | null        time of the last event (what the
                                        UPDATED column shows)

  Does that field list work?

> ok

• That settles the fields. What's left is how --json behaves at the edges, and
  the exact wording of the help text and docs:

  [behavior with filters and empty boards, flag help, doc updates]

  Two small cleanups I noticed nearby

   1 The --since help mentions this-week and this-month, but its error message
     only lists today and h/d/w/m/y. I'd change the error to [...] list every
     accepted spec and update the test that pins the old wording.
   2 The imp list bullet in introspection.md says the board is "active in-flight
     and draft quests, plus the backlog". It leaves out the completed section
     that the skill doc mentions. I'd bring it in line with the skill's wording.

  Does all of that sound right, cleanups included?

> ok

• Here's the full design in one place:

  [goal, shape, behavior, docs and copy, cleanups, tests]

  Shall I start building it? If you'd rather hold off, I'll set it aside and you
  can pick it up later with imp resume seru99.

> ok

• I wrote the spec and set up a separate working copy for the build. The change
  now goes through the full design review, then planning, building and testing.

  [spec]                       ✓  (1 round of review)
  [plan]                       ✓  (1 round of review)
  [implementation]             ✓  (each commit reviewed individually)
  [full test suite]            ✓
  [final code review]          ✓
  [integration]                ✓

Merged seru99 into master.
```
Mid-run screenshot:

<p align="center">
  <img src="docs/images/hero-run.png" alt="imp mid-run: the live status bar shows the quest, phase, plan and task counters, model, and context gauge, above a coder subagent's own gauge">
</p>

<p align="center">
  <em>Mid-flight: task 3 of 4, with a <code>coder</code> subagent working under a context budget.</em>
</p>


Stats:
- 34m runtime
- human input: 1 opening prompt, 4 approvals
- 5 commits, 9 files, +341/-38
- 14 new tests
- 0.9% of a weekly Claude Ultra quota

It produced the following logically sequenced, independently reviewable
commits:

```console
$ git log --oneline --reverse --no-merges --grep='Quest: seru99'
72c65ca6 list: name every --since keyword in the error
b6291026 list: split the board gather stage from text rendering
0e2473cb list: add --json
c9d433fe docs: document imp list --json
09e59f13 docs: list completed quests in the README imp list example
```

Note that only **one of the five commits implements the actual feature**:

```console
0e2473cb list: add --json
```

It's preceded by a **refactoring** that splits gathering the board's rows from
rendering them, so the text and JSON output read from one source, and a
**cleanup** of an error message it noticed along the way:

```console
72c65ca6 list: name every --since keyword in the error
b6291026 list: split the board gather stage from text rendering
```

And the remaining two are **proactive documentation updates**, one of them a
stale README example that a reviewer flagged mid-build:

```console
c9d433fe docs: document imp list --json
09e59f13 docs: list completed quests in the README imp list example
```

Full quest log and quota summary: [`docs/examples/seru99.md`](docs/examples/seru99.md)

### Fixing a bug

*[Edited for brevity.  Full transcript [here](docs/examples/vida80.md).]*

```console
$ imp new "why did kelu74 fail"
vida80 · new

> why did kelu74 fail

  [reads kelu74's event log and the provider code]

• kelu74 ("Document final merge quest trailers") didn't fail because of
  anything in its own work. It hit a crash inside imp's Codex provider code and
  paused itself with the reason "unknown".

  [...]

  imp started a new Codex turn to deliver that reminder. 17 ms later, before the
  model could answer, a bare Python assert failed inside imp. The logged error
  is AssertionError with an empty message: "unclassified provider error: ".

  [...]

  A fix would have two parts: reproduce the crash with a throwaway test that
  re-prompts a Codex step and fix the check that fires, and include the
  traceback in "unknown" error events. Want me to scope a fix covering both?

> ok

• I'll call this "Fix Codex re-prompt crash". Next I'll run two things in
  parallel: a throwaway reproduction to find which check actually fires, and a
  code map of how "unknown" errors are recorded and shown.

  spiker · claude-opus-5-5 · 8m 11s

  explorer · claude-sonnet-5-5 · 1m 4s

• The reproduction pinned it down. The check that fires is
  src/imp/providers/codex_exec.py:132. When a Codex turn ends, imp exits its
  event loops with a plain break, which leaves the stream generators open. So
  the cleanup that kills and unregisters the old Codex process only runs later,
  whenever Python's garbage collector gets around to closing them. If imp
  re-prompts the agent right away, as it does for the "you forgot to finish"
  reminder, the new Codex process registers while the old one is still held,
  and the check fails. [...] The reproduction failed 3 of 3 runs.

  A fix that passed the reproduction 3 of 3 runs closes the generators
  explicitly, in two places:

   • src/imp/agents/agent_loop.py: wrap session.events() in
     contextlib.aclosing.
   • src/imp/providers/observe_session.py: wrap events()'s self._receive() the
     same way.

  [...]

  Should the crash fix be the explicit close in both places, with the race
  reproduced as the regression test?

> ok

  [proposes capturing a traceback on "unknown" provider errors and showing it
   under imp log --full; then a recap of the whole design]

> ok
> ok
> ok

  [spec]                       ✓  (2 rounds of review)
  [plans 1-2]                  ✓  (1 round of review each)
  [implementation]             ✓
  [full test suite]            ✓
  [final code review]          ✓
  [integration]                ✓

Merged vida80 into master.
```

Stats:
- 1h 3m runtime
- human input: 1 opening prompt, 5 approvals
- 3 commits, 15 files, +325/-85
- 9 new tests, including a regression test that reproduces the crash against a
  fake codex executable
- 0.9% of a weekly Claude Ultra quota

It debugged the issue, reproduced it with a throwaway spike, and then (after
getting the user's approval) produced the following logically sequenced,
independently reviewable commits:

```console
$ git log --oneline --reverse --no-merges --grep='Quest: vida80'
62b972f7 agents: close per-turn event generators before re-prompting
93be0593 providers: record a traceback tail on unknown provider errors
1b66a556 cli: show error tracebacks in imp log --full
```

It fixed the crash in the first commit:

```console
62b972f7 agents: close per-turn event generators before re-prompting
```

Then it **proactively improved error reporting** so the next unclassified
provider error can be traced from the log alone:

```console
93be0593 providers: record a traceback tail on unknown provider errors
1b66a556 cli: show error tracebacks in imp log --full
```

Full quest log and quota summary: [`docs/examples/vida80.md`](docs/examples/vida80.md)

## Token costs

The above [examples](#examples) took 30-60 minutes of clock time.  The
[default workflow](#default-workflow) can be quite a bit more heavyweight than
a typical Claude session.  Most of the time and tokens go towards design and
planning (through multiple rounds of review), before any code is written.  That
up-front effort makes the code less buggy, more maintainable, and more likely
to meet the user's expectations, with fewer "surprises".

Quality isn't cheap.  When using the default workflow, **a Claude Max or Ultra
(or ChatGPT Pro) subscription is recommended.**  Subscriptions are *much*
cheaper per token than API billing.  With Anthropic API billing, the `--json`
feature above would have cost ~$9, and the bug fix (including debugging)
would have cost ~$15.

The example runs each cost about ~1% of my weekly Claude Ultra window.  That
means *roughly* 100 quests per week for a single subscription.

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

Working on imp itself? Install with `uv tool install --editable .` instead, so changes merged in your checkout take effect without reinstalling.

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
imp show <id>                    # intent, status, timing, result, and run ledger
imp log <id>                     # readable story of the run
imp log --summary <id>           # per-step ledger of time, tokens, and quota
imp search "rate limit"          # active, backlog, and history quests
```

Every commit log produced by the default workflow carries a `Quest: <id>`
trailer.  That makes it possible to associate every line of code with the
quest and conversation that created it:

```bash
git blame path/to/file
git log -1 --format='%(trailers:key=Quest,valueonly)' <commit>
imp log <quest-id>
```

You can also see all in-progress, backlog, and completed quests:

```console
$ imp list
ID      TITLE                                                  STATUS    UPDATED
fego87  README that shows imp working                          running   0s

backlog:
jara67  Revisit migrating codex to the official app-server S…  backlog   10d
timo83  Make test_cmd_slow false skip the slow gate            backlog   36d
jiro48  Restore archived-row titles and intents under the sa…  backlog   36d
  … 15 rows elided …

completed:
seru99  JSON output for imp list                               merged    2h
gepa93  Wire the completion nudge into codex live tests        merged    10h
sobi11  Adopt filelock 4.0                                     merged    10h
vuka12  Cut redundant review ceremony                          merged    10h
  … 867 rows elided …
```

And full fast search is supported:

```console
$ imp search "merge queue"
433 matches for "merge queue"

vuka12 · Cut redundant review ceremony · history · 10h
  … "wait-for-head": "waiting in merge queue", "reconciler": "resol …
  … body). _IR_SIG = "HEAD of the merge queue" def _prompt_carries_ir(**kw …

fesa14 · Wait for dependencies outside the provider call · history · 6d
  … ** for the next quest in the **merge queue**, i.e. the next quest waiting …
  … acquire step, not yet head in merge queue → `raise AwaitingUserInput()` …
  … thing that happens when in the merge queue (and what happens today for cr …
  … +3 more

gevu23 · Resume without replaying the integrate phase · history · 10d
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
