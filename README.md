# imp

**Stop babysitting your coding agent.**

![license](https://img.shields.io/badge/license-Apache--2.0-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![status](https://img.shields.io/badge/status-pre--release-orange)

imp is a batteries-included, declarative agent workflow that interviews you to
settle the design, then takes a code change from spec to merge without needing
you to supervise every step. Detailed specs and implementation plans carry your
decisions across a fresh context for every step. Tests are written first, every
artifact is independently reviewed, and the finished change is checked against
its acceptance criteria before merge.

Every change runs as its own isolated, resumable **quest**. The complete agent
conversation is archived with it, so you can trace the code back to the reasoning
that produced it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/imp-interview-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/imp-interview-light.png">
  <img alt="imp interviewing the user about a change: it reads the code, proposes a design, and asks the one question it cannot answer itself" src="docs/images/imp-interview-dark.png">
</picture>

I am a Linux kernel maintainer, so I have fairly boring ideas about what makes a
change trustworthy: a written contract, review by somebody who did not write it,
tests that were seen failing before they passed, and a record I can inspect
afterward. imp turns those habits into an executable workflow.

It does not make the models smarter. It gives them a process that makes mistakes
harder to hide and easier to send back.

imp has built itself since June 2026. At the time of writing, 79% of the lines in
`src/` arrived on a branch that imp specified, planned, implemented, reviewed,
tested, and merged.

## Try it

imp needs Linux, Git, Python 3.11 or newer, and
[bubblewrap](https://github.com/containers/bubblewrap). It can use an existing
Claude Code or Codex subscription, or call the Anthropic and OpenAI APIs directly.

```bash
git clone https://github.com/jpoimboe/imp
cd imp
python3 -m pip install -e .

cd /path/to/your/project
imp init
imp new "add rate limiting to the public API"
```

`imp init` inspects the repository and confirms the base branch, workflow, test
commands, worktree setup, and provider. `imp new` starts the interview. imp reads
the code before asking questions, proposes concrete approaches and tradeoffs, and
spikes throwaway code when the answer needs an experiment rather than an opinion.

When the design is settled, tell it to build. The quest leaves draft state, gets
its own branch and worktree, and runs to the next explicit human gate. You can
watch it, leave it alone, interrupt it, or resume it later from the event log.

## What the default workflow does

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
  converge -. gap found .-> plan
  integrate -. rework .-> plan
```

The shipped workflow is deliberately opinionated:

- **Interview before implementation.** Your judgment goes in while changing the
  design is still cheap. imp asks about decisions it cannot make from the code,
  not facts it can discover itself.
- **Write the contract down.** A spec defines the result and its acceptance
  criteria. An implementation plan names the code and tests needed to get there.
  Both are reviewed before implementation starts.
- **Use fresh context.** Every step is a separate model invocation with only the
  context it needs. The spec and plan carry decisions forward; reviewers do not
  inherit the author's reasoning or assumptions.
- **Write tests first.** New tests must be observed failing for the expected
  reason before implementation. A test that never proved it could fail is not
  evidence.
- **Make reviewers independent.** The agents reviewing the spec, plan, and code
  did not write them. They are told to inspect the repository and verify claims,
  not agree with the previous agent.
- **Converge on the spec.** After implementation, imp checks the whole change
  against the acceptance criteria, runs the slow tests, and routes gaps back into
  planning or implementation.
- **Verify at the integration point.** imp takes a merge-queue slot, rebases onto
  the current base, reruns verification, reviews the integrated result, and stops
  at the merge gate.

This is the batteries-included workflow, not engine policy. Its phases, routes,
gates, model tiers, scripts, and agent prompts are ordinary files in your
repository. Keep it, change it, or replace it. The engine only executes the graph
and records what happened.

## A quest leaves a trail

A quest is one unit of work from the opening intent to the final merge. Before
you choose to build, it is only a conversation and some notes: no branch and no
worktree. You can build it, park it in the backlog, or discard it.

Once live, the quest owns its worktree, artifacts, workflow snapshot, and
append-only event log. Every prompt, response, tool call, result, decision, test
run, and gate outcome goes into that log. State is rebuilt from the log rather
than held only in memory, which is why an interrupted run can resume without
pretending the interrupted step never happened.

When the quest finishes, its conversation and artifacts are archived and remain
searchable. The commit is not the only surviving explanation of the change:

```bash
imp list                         # active work, drafts, and backlog
imp show <id>                    # intent, status, timing, and result
imp log <id>                     # readable story of the run
imp events <id> --agent coder    # the coder's event stream
imp search "rate limit"          # live and archived conversations
imp events search "pytest"       # tool calls, results, and agent reasoning
```

That is what makes provenance useful here. You can start from a line of code,
find the quest that introduced it, and read the archived conversation that led
to it.

## What happens when a test fails

Here is a real excerpt from `imp log`. A verification gate went red partway
through a change:

```text
↩ verify → task-lead  (Gate `./scripts/run-verify.sh fast` failed (exit 2))

• The failing test spawns a real sandbox and asserts that imp --help resolves the
  worktree's CLI via a PYTHONPATH shadow. My diff only changes bytes the archive
  progress helper writes to a TTY stderr; this test never touches the archive and
  captures a non-TTY subprocess, where the helper was and remains silent. Not
  attributable to my change either way, so I'm recording it as a tolerated
  failure rather than inventing a fix.
```

It did not edit the unrelated test, invent a fix, or keep retrying until
something happened to turn green. It recorded the toleration, which forced the
failure to surface again at the merge gate. There it found the existing backlog
quest tracking that failure before closing the change and merging it.

That run took 26 minutes, 21 steps, and two keystrokes from me. It cost $6.10 at
provider API list prices. Across imp's 40 most recent merged changes when these
numbers were collected, the median was $46, the middle half ranged from $27 to
$96, and the largest was $339. Those prices cover the whole process: interview,
specification, planning, independent reviews, implementation, tests, rebase,
integration review, and merge.

<details>
<summary><code>imp log --summary</code> for that run</summary>

```text
default  ·  fori10  ·  merged  ·  26m 25s  ·  4.2M  ·  $6.10
threshold
  threshold                           7m 50s  790.3k  $2.21
  scope-code-check                        0s       —      —
plan
  plan-writer                          2m 8s  726.1k  $0.76
  plan-verify-commit                      0s       —      —
  plan-finalized                          0s       —      —
implement
  task-lead                           4m 32s  435.8k  $0.51
    ↳ coder (writing code)            1m 10s  173.2k  $0.14
    ↳ code-reviewer (reviewing code)  1m 40s  177.5k  $0.19
  verify                              2m 13s       —      —
  task-lead                              57s  186.5k  $0.36
  check-next-plan                         0s       —      —
converge
  gap-check                           1m 11s  260.2k  $0.32
  quest-review                         3m 0s  945.3k  $0.87
  deferred-sweep                         19s   57.6k  $0.14
  converge-verify                     1m 28s       —      —
  deferred-sweep                         40s  101.5k  $0.17
integrate
  wait-for-head                           0s       —      —
  integrate-rebase                        0s       —      —
  summarizer                             22s   62.6k  $0.06
  finalize                            1m 20s  286.8k  $0.36
  cleanup-merge                           0s       —      —
  assert-stamped                          0s       —      —
  merge-to-base                           0s       —      —
```

Script steps that only route on an exit code cost nothing, and appear that way.
Run `imp log --summary <id>` to get the same accounting for your own quest.

</details>

## Isolation and security boundaries

Each live quest uses a separate git worktree. Nothing touches your checkout,
index, or stash, and several quests can run at once.

A worktree is not a security boundary because worktrees share one `.git`. imp
therefore runs agent shell commands under bubblewrap, in separate user, IPC, and
UTS namespaces. The filesystem is read-only outside an explicit writable set;
credentials are replaced with empty mounts; and shared Git escape paths such as
hooks, config, the index, and refs outside the quest's namespace are remounted
read-only. This fails closed: no `bwrap`, no run.

Two limits are worth stating plainly. Network access is allowed by default; set
`sandbox.network: false` to disable it. File-editing tools enforce the same path
policy in-process, so the kernel-level guarantee applies to shell commands.

## Documentation

- [What is imp?](docs/guide/what-is-imp.md) explains quests, phases, and drafts.
- [Features](docs/guide/features.md) covers review gates, project memory,
  per-role model selection, and remote control.
- [Lifecycle](docs/guide/lifecycle.md) covers statuses, the backlog, the archive,
  and what happens when you leave a conversation.
- [Introspection](docs/guide/introspection.md) covers logs, events, history, and
  search.
- [Embedding](docs/guide/embedding.md) documents the supported Python API.
- [Architecture](docs/internals/architecture.md) describes the engine and the
  bundled workflows.

## Status and contributing

imp is pre-release. The engine and default workflow are in daily use, but
interfaces may change before the first tagged release.

imp drives the official `claude` and `codex` binaries through their published
agent SDKs, or uses the Anthropic and OpenAI APIs directly. It is an independent
harness, not an Anthropic or OpenAI product, and carries no endorsement from
either. Most production use so far has been with Claude Code.

Issues and pull requests are welcome. From a clone, run `make sync-env` to build
the development environment from `uv.lock`, then run `make check` before opening
a pull request.

## License

Apache 2.0. See [LICENSE](LICENSE). Third-party attribution is in
[NOTICE](NOTICE).
