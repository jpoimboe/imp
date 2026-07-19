# Systematic Debugging

## Overview

Random/quick fixes waste time, spawn new bugs, and mask the underlying issue.

**Core principle:** ALWAYS find the root cause before any fix. A symptom fix is a failure. **Violating the letter of this process violates its spirit.**

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

Haven't completed Phase 1? You cannot propose fixes.

## When to Use

ANY technical issue: test failures, production bugs, unexpected behavior, performance problems, build failures, integration issues.

**ESPECIALLY when guessing is tempting:** under time pressure / emergency; an "obvious" quick fix; after already trying multiple fixes; a previous fix didn't work; you don't fully understand the issue.

**Don't skip because:** the issue seems simple (simple bugs have root causes too); you're in a hurry (rushing guarantees rework); someone wants it fixed NOW (systematic beats thrashing).

## The Four Phases

Complete each phase before the next.

### Phase 1: Root Cause Investigation — understand WHAT broke and WHY

BEFORE any fix:

1. **Read errors completely** — don't skip errors/warnings (they often hold the exact solution). Read full stack traces; note line numbers, file paths, error codes.
2. **Reproduce consistently** — can you trigger it reliably? Exact steps? Every time? If not reproducible, gather more data — don't guess.
3. **Check recent changes** — git diff / recent commits, new dependencies, config changes, environmental differences.
4. **Gather evidence in multi-component systems** (CI → build → signing, API → service → database): BEFORE proposing fixes, add diagnostic instrumentation at each component boundary — log data entering and exiting each component, verify environment/config propagation, check state at each layer. Run once to gather evidence of WHERE it breaks, then analyze it to identify the failing component, then investigate that component.

   Example (illustrative — log at each boundary):
   ```bash
   echo "workflow: IDENTITY=${IDENTITY:+SET}${IDENTITY:-UNSET}"   # secrets → workflow
   env | grep IDENTITY || echo "IDENTITY not in env"              # workflow → build
   security find-identity -v                                      # build → signing
   codesign --sign "$IDENTITY" --verbose=4 "$APP"                 # signing
   ```
   This reveals which layer fails.
5. **Trace data flow** when the error is deep in the call stack — where does the bad value originate, what called it with that value, keep tracing up to the source, fix at the source not the symptom. See **Root Cause Tracing** below.

### Phase 2: Pattern Analysis — identify the differences

1. **Find working examples** — locate similar working code in the same codebase; what works that resembles what's broken?
2. **Compare against references** — read the reference implementation COMPLETELY (every line, don't skim); understand the pattern fully before applying.
3. **Identify differences** — list every difference between working and broken, however small; don't assume "that can't matter".
4. **Understand dependencies** — what components, settings, config, environment this needs, and what assumptions it makes.

### Phase 3: Hypothesis and Testing — a confirmed or newly-formed hypothesis

1. **Form one hypothesis, written down** — specific: "I think X is the root cause because Y".
2. **Test minimally** — the smallest change to test it, one variable at a time; don't fix multiple things at once.
3. **Verify before continuing** — worked → Phase 4; didn't → form a NEW hypothesis, don't stack more fixes.
4. **When you don't know** — say "I don't understand X"; don't pretend; ask for help / research more.

### Phase 4: Implementation — bug resolved and tests pass

1. **Create a failing test first** — simplest reproduction (automated test, or a one-off script if no framework); you MUST have it before fixing; write it with TDD discipline.
2. **Implement a single fix** for the identified root cause — one change, no "while I'm here" improvements, no bundled refactoring.
3. **Verify the fix** — test passes now, no other tests broke, issue actually resolved.
4. **If it doesn't work: STOP** and count fixes tried. If < 3, return to Phase 1 and re-analyze with the new info. If ≥ 3, STOP and question the architecture (below). Never attempt fix #4 without an architectural discussion.
5. **3+ fixes failed = wrong architecture, not a failed hypothesis.** Signals: each fix reveals new shared state/coupling/problem in a different place; fixes require "massive refactoring"; each fix creates new symptoms elsewhere. Question fundamentals — is the pattern fundamentally sound, are we continuing through sheer inertia, should we refactor vs. keep fixing symptoms? Discuss with the user before more fixes.

## Stop Guessing — Return to Phase 1

**Red-flag thoughts** (each means STOP, return to Phase 1): "quick fix for now, investigate later"; "just try changing X and see"; "add multiple changes, run tests"; "skip the test, I'll manually verify"; "it's probably X, let me fix that"; "I don't fully understand but this might work"; "pattern says X but I'll adapt it differently"; listing fixes as the main problems without investigation; proposing solutions before tracing data flow; "one more fix attempt" when already tried 2+; each fix revealing a new problem in a different place. (If 3+ fixes failed, question the architecture — Phase 4.5.)

**User redirections** (each means STOP, return to Phase 1): "Is that not happening?" (you assumed without verifying); "Will it show us…?" (you should have added evidence gathering); "Stop guessing" (proposing fixes without understanding); "We're stuck?" (your approach isn't working).

**Rationalizations:**

| Excuse | Reality |
|--------|---------|
| "Issue is simple, don't need process" | Simple issues have root causes too; process is fast for them. |
| "Emergency, no time for process" | Systematic debugging is FASTER than guess-and-check thrashing. |
| "Just try this first, then investigate" | The first fix sets the pattern. Do it right from the start. |
| "I'll write the test after confirming the fix" | Untested fixes don't stick. Test-first proves it. |
| "Multiple fixes at once saves time" | Can't isolate what worked; causes new bugs. |
| "Reference too long, I'll adapt the pattern" | Partial understanding guarantees bugs. Read it completely. |
| "I see the problem, let me fix it" | Seeing symptoms ≠ understanding root cause. |
| "One more fix attempt" (after 2+ failures) | 3+ failures = architectural problem. Question the pattern, don't fix again. |

## When Process Reveals "No Root Cause"

If investigation truly reveals an environmental / timing-dependent / external cause, you've completed the process: document what you investigated, implement appropriate handling (retry, timeout, error message), and add monitoring/logging for future investigation. But 95% of "no root cause" cases are incomplete investigation.

## Supporting Techniques

### Root Cause Tracing

Bugs often manifest deep in the call stack; the instinct to fix where the error appears treats a symptom. **Core principle:** trace backward through the call chain to the original trigger, then fix at the source.

**Use when:** the error happens deep in execution (not at entry point), the stack trace shows a long call chain, it's unclear where invalid data originated, or you need to find which test/code triggers it.

**Process:** observe the symptom → find the immediate cause → ask what called this and trace the chain upward → keep tracing up, noting the value passed → find the original trigger.

Worked example:
```
Symptom: git init failed in ~/project/packages/core
Chain:  WorktreeManager.create_session_worktree(project_dir, session_id)
          → Session.initialize_workspace() → Session.create()
          → test at Project.create()
Value:  project_dir = '' (empty) → empty cwd resolves to os.getcwd() → source dir!
Trigger: context = setup_core_test()  # {'temp_dir': ''}
         Project.create('name', context['temp_dir'])  # accessed before setup
```

When you can't trace manually, add stack-trace instrumentation — print the argument and `traceback.format_stack()` to stderr, then run with output captured (e.g. `pytest 2>&1 | grep -A 20 'DEBUG git init'`).

**Key principle: NEVER fix just where the error appears** — trace back to the original trigger.

### Condition-Based Waiting

Arbitrary delays in flaky tests create race conditions that pass on fast machines but fail under load / in CI. **Core principle:** wait for the actual condition you care about, not a guess about how long it takes.

**Use when:** tests have arbitrary delays / fixed timeouts, are flaky, time out in parallel, or await async operations.

**Don't use when** testing actual timing behavior (debounce, throttle intervals); and always document WHY when using an arbitrary timeout.

```python
# BAD: guessing at timing
time.sleep(0.05); result = get_result()

# GOOD: wait for the condition
wait_for(lambda: get_result() is not None, "result to appear")
```

Use a generic polling helper `wait_for(condition, description, timeout_ms=5000)` that polls until the condition holds or raises a clear timeout error — it covers the common scenarios: wait for event, state, count, or file.

**Mistakes:** don't poll too fast (poll ~every 10ms, not 1ms); never omit a timeout (always include one with a clear error message); don't cache stale state before the loop — call the getter inside the loop for fresh data.

An arbitrary timeout is legitimate only for genuinely timed behavior: first wait for the triggering condition/event, then sleep the documented interval, with the reason noted:
```python
wait_for_event(manager, 'TOOL_STARTED')  # first: wait for the condition
time.sleep(0.2)  # then: timed behavior — 200ms = 2 ticks, documented
```

### Defense-in-Depth Validation

A single validation point can be bypassed by other code paths, refactoring, or mocks. **Core principle:** validate at EVERY layer the data passes through to make the bug structurally impossible. Single check = "we fixed the bug"; multiple layers = "we made the bug impossible".

The four layers each catch different cases (illustrative):
1. **Entry-point** — reject obviously invalid input at the API boundary (e.g. empty / nonexistent / non-directory path → raise).
2. **Business-logic** — ensure the data makes sense for this operation (e.g. `project_dir` required for workspace init).
3. **Environment guards** — prevent dangerous operations in specific contexts (e.g. during tests, refuse a `git init` outside temp dirs).
4. **Debug instrumentation** — capture context (directory, cwd) for forensics.

**Apply:** trace the data flow (where the bad value originates and where it's used), map every checkpoint it passes through, add validation at each layer, and test each layer (try to bypass layer 1, verify layer 2 catches it). Don't stop at one validation point.

## Real-World Impact

Systematic vs random thrashing: ~15–30 minutes vs 2–3 hours to fix; ~95% vs 40% first-time fix rate; near-zero vs common new bugs introduced.
