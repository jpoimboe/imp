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
sufficiently, it runs unattended from spec, plan, code, test, review, all the
way through merge, so you're free to step away from the computer.  Or kick off
another quest for the same project in parallel, as imp runs in **resumable,
isolated git worktrees.**

All agent-made judgment calls are raised as open items to the user at the merge
gate, where the human can approve, send back for rework, or open a followup
quest for further improvements.

Imp's uses **fresh LLM context and independent reviewers** so each agent can
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

The default workflow can be a bit token-intensive due to the extra thinking
involved with detailed specs, plans, and multiple reviews.  Quality,
maintainable, functional, well-tested code isn't cheap.  **Claude Max (or
ChatGPT Pro) subscription recommended for the default workflow.**  Local models
are not (yet) supported.  Pull requests welcome :-)

For a lighter weight approach, custom workflows can be created, while still
taking advantage of imp's useful [engine features](#engine-features).

imp is currently Linux-only.  Support for macOS is planned.

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
