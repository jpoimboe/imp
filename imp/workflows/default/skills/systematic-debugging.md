# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask underlying issues.

**Core principle:** ALWAYS find root cause before attempting fixes. Symptom fixes are failure.

**Violating the letter of this process is violating the spirit of debugging.**

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

If you haven't completed Phase 1, you cannot propose fixes.

## When to Use

Use for ANY technical issue:
- Test failures
- Bugs in production
- Unexpected behavior
- Performance problems
- Build failures
- Integration issues

**Use this ESPECIALLY when:**
- Under time pressure (emergencies make guessing tempting)
- "Just one quick fix" seems obvious
- You've already tried multiple fixes
- Previous fix didn't work
- You don't fully understand the issue

**Don't skip when:**
- Issue seems simple (simple bugs have root causes too)
- You're in a hurry (rushing guarantees rework)
- Manager wants it fixed NOW (systematic is faster than thrashing)

## The Four Phases

You MUST complete each phase before proceeding to the next.

### Phase 1: Root Cause Investigation

**BEFORE attempting ANY fix:**

1. **Read Error Messages Carefully**
   - Don't skip past errors or warnings
   - They often contain the exact solution
   - Read stack traces completely
   - Note line numbers, file paths, error codes

2. **Reproduce Consistently**
   - Can you trigger it reliably?
   - What are the exact steps?
   - Does it happen every time?
   - If not reproducible → gather more data, don't guess

3. **Check Recent Changes**
   - What changed that could cause this?
   - Git diff, recent commits
   - New dependencies, config changes
   - Environmental differences

4. **Gather Evidence in Multi-Component Systems**

   **WHEN system has multiple components (CI → build → signing, API → service → database):**

   **BEFORE proposing fixes, add diagnostic instrumentation:**
   ```
   For EACH component boundary:
     - Log what data enters component
     - Log what data exits component
     - Verify environment/config propagation
     - Check state at each layer

   Run once to gather evidence showing WHERE it breaks
   THEN analyze evidence to identify failing component
   THEN investigate that specific component
   ```

   **Example (multi-layer system):**
   ```bash
   # Layer 1: Workflow
   echo "=== Secrets available in workflow: ==="
   echo "IDENTITY: ${IDENTITY:+SET}${IDENTITY:-UNSET}"

   # Layer 2: Build script
   echo "=== Env vars in build script: ==="
   env | grep IDENTITY || echo "IDENTITY not in environment"

   # Layer 3: Signing script
   echo "=== Keychain state: ==="
   security list-keychains
   security find-identity -v

   # Layer 4: Actual signing
   codesign --sign "$IDENTITY" --verbose=4 "$APP"
   ```

   **This reveals:** Which layer fails (secrets → workflow ✓, workflow → build ✗)

5. **Trace Data Flow**

   **WHEN error is deep in call stack:**

   - Where does bad value originate?
   - What called this with bad value?
   - Keep tracing up until you find the source
   - Fix at source, not at symptom

   See the **Root Cause Tracing** section below for the complete backward tracing technique.

### Phase 2: Pattern Analysis

**Find the pattern before fixing:**

1. **Find Working Examples**
   - Locate similar working code in same codebase
   - What works that's similar to what's broken?

2. **Compare Against References**
   - If implementing pattern, read reference implementation COMPLETELY
   - Don't skim - read every line
   - Understand the pattern fully before applying

3. **Identify Differences**
   - What's different between working and broken?
   - List every difference, however small
   - Don't assume "that can't matter"

4. **Understand Dependencies**
   - What other components does this need?
   - What settings, config, environment?
   - What assumptions does it make?

### Phase 3: Hypothesis and Testing

**Scientific method:**

1. **Form Single Hypothesis**
   - State clearly: "I think X is the root cause because Y"
   - Write it down
   - Be specific, not vague

2. **Test Minimally**
   - Make the SMALLEST possible change to test hypothesis
   - One variable at a time
   - Don't fix multiple things at once

3. **Verify Before Continuing**
   - Did it work? Yes → Phase 4
   - Didn't work? Form NEW hypothesis
   - DON'T add more fixes on top

4. **When You Don't Know**
   - Say "I don't understand X"
   - Don't pretend to know
   - Ask for help
   - Research more

### Phase 4: Implementation

**Fix the root cause, not the symptom:**

1. **Create Failing Test Case**
   - Simplest possible reproduction
   - Automated test if possible
   - One-off test script if no framework
   - MUST have before fixing
   - Use TDD discipline when writing the failing test

2. **Implement Single Fix**
   - Address the root cause identified
   - ONE change at a time
   - No "while I'm here" improvements
   - No bundled refactoring

3. **Verify Fix**
   - Test passes now?
   - No other tests broken?
   - Issue actually resolved?

4. **If Fix Doesn't Work**
   - STOP
   - Count: How many fixes have you tried?
   - If < 3: Return to Phase 1, re-analyze with new information
   - **If ≥ 3: STOP and question the architecture (step 5 below)**
   - DON'T attempt Fix #4 without architectural discussion

5. **If 3+ Fixes Failed: Question Architecture**

   **Pattern indicating architectural problem:**
   - Each fix reveals new shared state/coupling/problem in different place
   - Fixes require "massive refactoring" to implement
   - Each fix creates new symptoms elsewhere

   **STOP and question fundamentals:**
   - Is this pattern fundamentally sound?
   - Are we "sticking with it through sheer inertia"?
   - Should we refactor architecture vs. continue fixing symptoms?

   **Discuss with the user before attempting more fixes.**

   This is NOT a failed hypothesis - this is a wrong architecture.

## Red Flags - STOP and Follow Process

If you catch yourself thinking:
- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "Add multiple changes, run tests"
- "Skip the test, I'll manually verify"
- "It's probably X, let me fix that"
- "I don't fully understand but this might work"
- "Pattern says X but I'll adapt it differently"
- "Here are the main problems: [lists fixes without investigation]"
- Proposing solutions before tracing data flow
- **"One more fix attempt" (when already tried 2+)**
- **Each fix reveals new problem in different place**

**ALL of these mean: STOP. Return to Phase 1.**

**If 3+ fixes failed:** Question the architecture (see Phase 4.5)

## Signals You're Doing It Wrong

**Watch for these redirections from the user:**
- "Is that not happening?" - You assumed without verifying
- "Will it show us...?" - You should have added evidence gathering
- "Stop guessing" - You're proposing fixes without understanding
- "We're stuck?" (frustrated) - Your approach isn't working

**When you see these:** STOP. Return to Phase 1.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Issue is simple, don't need process" | Simple issues have root causes too. Process is fast for simple bugs. |
| "Emergency, no time for process" | Systematic debugging is FASTER than guess-and-check thrashing. |
| "Just try this first, then investigate" | First fix sets the pattern. Do it right from the start. |
| "I'll write test after confirming fix works" | Untested fixes don't stick. Test first proves it. |
| "Multiple fixes at once saves time" | Can't isolate what worked. Causes new bugs. |
| "Reference too long, I'll adapt the pattern" | Partial understanding guarantees bugs. Read it completely. |
| "I see the problem, let me fix it" | Seeing symptoms ≠ understanding root cause. |
| "One more fix attempt" (after 2+ failures) | 3+ failures = architectural problem. Question pattern, don't fix again. |

## Quick Reference

| Phase | Key Activities | Success Criteria |
|-------|---------------|------------------|
| **1. Root Cause** | Read errors, reproduce, check changes, gather evidence | Understand WHAT and WHY |
| **2. Pattern** | Find working examples, compare | Identify differences |
| **3. Hypothesis** | Form theory, test minimally | Confirmed or new hypothesis |
| **4. Implementation** | Create test, fix, verify | Bug resolved, tests pass |

## When Process Reveals "No Root Cause"

If systematic investigation reveals issue is truly environmental, timing-dependent, or external:

1. You've completed the process
2. Document what you investigated
3. Implement appropriate handling (retry, timeout, error message)
4. Add monitoring/logging for future investigation

**But:** 95% of "no root cause" cases are incomplete investigation.

## Supporting Techniques

### Root Cause Tracing

Bugs often manifest deep in the call stack. Your instinct is to fix where the error appears, but that's treating a symptom.

**Core principle:** Trace backward through the call chain until you find the original trigger, then fix at the source.

**Use when:**
- Error happens deep in execution (not at entry point)
- Stack trace shows long call chain
- Unclear where invalid data originated
- Need to find which test/code triggers the problem

**The Tracing Process:**

1. **Observe the Symptom**
   ```
   Error: git init failed in ~/project/packages/core
   ```

2. **Find Immediate Cause**
   What code directly causes this?

3. **Ask: What Called This?**
   Trace the call chain upward:
   ```
   WorktreeManager.create_session_worktree(project_dir, session_id)
     → called by Session.initialize_workspace()
     → called by Session.create()
     → called by test at Project.create()
   ```

4. **Keep Tracing Up**
   What value was passed?
   - `project_dir = ''` (empty string!)
   - Empty string as `cwd` resolves to `os.getcwd()`
   - That's the source code directory!

5. **Find Original Trigger**
   Where did empty string come from?
   ```python
   context = setup_core_test()  # Returns {'temp_dir': ''}
   Project.create('name', context['temp_dir'])  # Accessed before setup!
   ```

**Adding Stack Traces for tracing:**

When you can't trace manually, add instrumentation:

```python
import traceback

def git_init(directory: str):
    print(f'DEBUG git init: directory={directory!r}', file=sys.stderr)
    print(f'DEBUG stack:\n{traceback.format_stack()}', file=sys.stderr)
    # ... proceed
```

Run with output captured:
```bash
pytest 2>&1 | grep -A 20 'DEBUG git init'
```

**Key principle: NEVER fix just where the error appears.** Trace back to find the original trigger.

### Condition-Based Waiting

Flaky tests often guess at timing with arbitrary delays. This creates race conditions where tests pass on fast machines but fail under load or in CI.

**Core principle:** Wait for the actual condition you care about, not a guess about how long it takes.

**Use when:**
- Tests have arbitrary delays (`time.sleep`, fixed timeouts)
- Tests are flaky (pass sometimes, fail under load)
- Tests timeout when run in parallel
- Waiting for async operations to complete

**Don't use when:**
- Testing actual timing behavior (debounce, throttle intervals)
- Always document WHY if using arbitrary timeout

**Core pattern:**

```python
# BAD: Guessing at timing
time.sleep(0.05)
result = get_result()
assert result is not None

# GOOD: Waiting for condition
wait_for(lambda: get_result() is not None, "result to appear")
result = get_result()
assert result is not None
```

**Generic polling function:**

```python
import time

def wait_for(condition, description, timeout_ms=5000):
    start = time.monotonic()
    while True:
        result = condition()
        if result:
            return result
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > timeout_ms:
            raise TimeoutError(
                f"Timeout waiting for {description} after {timeout_ms}ms"
            )
        time.sleep(0.01)  # Poll every 10ms
```

**Quick Patterns:**

| Scenario | Pattern |
|----------|---------|
| Wait for event | `wait_for(lambda: next((e for e in events if e['type'] == 'DONE'), None), "DONE event")` |
| Wait for state | `wait_for(lambda: machine.state == 'ready', "machine ready")` |
| Wait for count | `wait_for(lambda: len(items) >= 5, "5 items")` |
| Wait for file | `wait_for(lambda: os.path.exists(path), "file created")` |

**Common Mistakes:**
- Polling too fast (wastes CPU) — poll every 10ms, not 1ms
- No timeout (loops forever) — always include timeout with clear error
- Stale data (cache state before loop) — call getter inside loop for fresh data

**When arbitrary timeout IS correct:**
```python
# Tool ticks every 100ms - need 2 ticks to verify partial output
wait_for_event(manager, 'TOOL_STARTED')  # First: wait for condition
time.sleep(0.2)  # Then: wait for timed behavior — 200ms = 2 ticks, documented
```

### Defense-in-Depth Validation

When you fix a bug caused by invalid data, adding validation at one place feels sufficient. But that single check can be bypassed by different code paths, refactoring, or mocks.

**Core principle:** Validate at EVERY layer data passes through. Make the bug structurally impossible.

Single validation: "We fixed the bug"
Multiple layers: "We made the bug impossible"

Different layers catch different cases:
- Entry validation catches most bugs
- Business logic catches edge cases
- Environment guards prevent context-specific dangers
- Debug logging helps when other layers fail

**The Four Layers:**

**Layer 1: Entry Point Validation**
Purpose: Reject obviously invalid input at API boundary

```python
def create_project(name: str, working_directory: str):
    if not working_directory or not working_directory.strip():
        raise ValueError('working_directory cannot be empty')
    if not os.path.exists(working_directory):
        raise ValueError(f'working_directory does not exist: {working_directory}')
    if not os.path.isdir(working_directory):
        raise ValueError(f'working_directory is not a directory: {working_directory}')
    # ... proceed
```

**Layer 2: Business Logic Validation**
Purpose: Ensure data makes sense for this operation

```python
def initialize_workspace(project_dir: str, session_id: str):
    if not project_dir:
        raise ValueError('project_dir required for workspace initialization')
    # ... proceed
```

**Layer 3: Environment Guards**
Purpose: Prevent dangerous operations in specific contexts

```python
def git_init(directory: str):
    # In tests, refuse git init outside temp directories
    if os.environ.get('PYTEST_RUNNING'):
        normalized = os.path.realpath(directory)
        tmp_dir = os.path.realpath(tempfile.gettempdir())
        if not normalized.startswith(tmp_dir):
            raise RuntimeError(
                f'Refusing git init outside temp dir during tests: {directory}'
            )
    # ... proceed
```

**Layer 4: Debug Instrumentation**
Purpose: Capture context for forensics

```python
import logging

def git_init(directory: str):
    logging.debug('About to git init', extra={
        'directory': directory,
        'cwd': os.getcwd(),
    })
    # ... proceed
```

**Applying the Pattern:**

When you find a bug:
1. Trace the data flow — Where does bad value originate? Where used?
2. Map all checkpoints — List every point data passes through
3. Add validation at each layer — Entry, business, environment, debug
4. Test each layer — Try to bypass layer 1, verify layer 2 catches it

**Don't stop at one validation point.** Add checks at every layer.

## Real-World Impact

From debugging sessions:
- Systematic approach: 15-30 minutes to fix
- Random fixes approach: 2-3 hours of thrashing
- First-time fix rate: 95% vs 40%
- New bugs introduced: Near zero vs common

> Ported from superpowers (MIT, Jesse Vincent): systematic-debugging.
