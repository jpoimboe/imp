# Test-Driven Development (TDD)

## Overview

Write the test first. Watch it fail. Write minimal code to pass.

**Core principle:** if you didn't watch the test fail, you don't know it tests the right thing.

**Violating the letter of the rules is violating the spirit.**

## When to Use

**Always:** new features, bug fixes, refactoring, behavior changes.

**Exceptions (ask the user):** throwaway prototypes, generated code, config files.

Thinking "skip TDD just this once"? Stop — that's rationalization.

## The Iron Law

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

Wrote code before its test? Delete it, start over. No exceptions: don't keep it "for reference", don't "adapt" it, don't even look at it. Delete means delete — implement fresh from the tests.

## Red-Green-Refactor

### RED — write a failing test

Write one minimal test showing what should happen: one behavior, a clear name, real code (mocks only if unavoidable).

A clear name testing real behavior beats a vague name testing the mock:

```python
# GOOD — clear name, real behavior, one thing
def test_retries_failed_operations_3_times():
    attempts = []
    def op():
        attempts.append(1)
        if len(attempts) < 3: raise RuntimeError('fail')
        return 'success'
    assert retry_operation(op) == 'success'
    assert len(attempts) == 3

# BAD — vague name, asserts the mock's call_count, not behavior
def test_retry_works():
    m = Mock(side_effect=[RuntimeError(), RuntimeError(), 'success'])
    retry_operation(m)
    assert m.call_count == 3
```

### Verify RED — watch it fail (MANDATORY, never skip)

Run the focused test. Confirm it **fails** (not errors), the failure message is the one you expect, and it fails because the feature is missing (not a typo).

- **Passes already?** You're testing existing behavior — fix the test.
- **Errors?** Fix the error, rerun until it fails correctly.

### GREEN — minimal code

Write the simplest code that passes. Don't add features, refactor other code, or improve beyond the test (YAGNI) — no extra `backoff`/`jitter`/`timeout` params "for later".

### Verify GREEN — watch it pass (MANDATORY)

Run the tests. Confirm the test passes, other tests still pass, and output is pristine (no errors or warnings).

- **Test fails?** Fix the code, not the test.
- **Other tests fail?** Fix them now.

### REFACTOR — clean up (after green only)

Remove duplication, improve names, extract helpers. Keep tests green; add no behavior.

### Repeat

Write the next failing test for the next feature.

## Good Tests

- **Minimal** — one thing. An "and" in the name (`test_validates_email_and_domain`) means split it.
- **Clear name** — describes the behavior, not `test_1`.
- **Shows intent** — demonstrates the desired API.

## Acceptance Test Oracles

An acceptance test has **one authoritative oracle** — the assertion that proves the feature works — asserted strictly. Corroborating signals that depend on non-determinism (commit counts, agent counts in multi-task runs) use bounds (`>= N`), never exact equality. Keep the oracle strict; relax only the corroborating counts.

## Rationalizations

Every excuse for skipping test-first, and its reality:

| Excuse | Reality |
|--------|---------|
| "I'll test after" | Tests written after pass immediately — proving nothing. You never saw them catch the bug. |
| "Tests after achieve the same goals — spirit not ritual" | Tests-after ask "what does this do?"; tests-first ask "what should this do?" Tests-after are biased by the implementation you already wrote. |
| "Already manually tested it" | Ad-hoc, no record, can't re-run when code changes. |
| "Deleting X hours is wasteful" | Sunk cost — the time is gone. Unverified code is technical debt, not an asset. |
| "TDD is dogmatic, I'm being pragmatic" | TDD *is* pragmatic: finds bugs before commit, prevents regressions, documents behavior, enables refactoring. |
| "Too simple to test" | Simple code breaks; the test costs 30 seconds. |
| "Need to explore first" | Fine — throw the exploration away and start with TDD. |
| "Hard to test = design unclear" | Hard to test = hard to use. Listen to the test. |
| "Manual is faster" | Manual doesn't prove edge cases, and you re-test by hand on every change. |
| "Existing code has no tests" | You're improving it — add tests as you go. |
| "Keep it as reference" | You'll adapt it — that's testing after. Delete means delete. |

**Red flags** — code before test, a test that passes immediately, "tests added later", "can't explain why it failed", "this is different because…", "just this once". All resolve to the same action: **delete the code, start over with TDD.**

## Example: Bug Fix

**Bug:** empty email accepted. (Illustrative.)

```python
# RED — failing test
def test_rejects_empty_email():
    assert submit_form({'email': ''})['error'] == 'Email required'

# Verify RED: run the focused test →
#   FAIL: AssertionError: 'Email required' != None

# GREEN — simplest code to pass
def submit_form(data):
    if not data.get('email', '').strip():
        return {'error': 'Email required'}
    ...

# Verify GREEN: run the tests → PASSED

# REFACTOR: extract shared field validation only if a second field needs it.
```

## Verification Checklist

Before a success `complete_step`, confirm all of these — if you can't check every box, you skipped TDD, so start over:

- [ ] Every new function/method has a test.
- [ ] You watched each test fail first, for the expected reason (feature missing, not a typo).
- [ ] You wrote minimal code to pass.
- [ ] All tests pass; output is pristine (no errors or warnings).
- [ ] Tests use real code (mocks only if unavoidable); edge cases and errors are covered.
- [ ] Each test would fail if the behavior were wrong — not merely if the plan were not transcribed.

## When Stuck

| Problem | Solution |
|---------|----------|
| Don't know how to test | Write the wished-for API / assertion first; ask the user. |
| Test too complicated | The design is too complicated — simplify the interface. |
| Must mock everything | Too coupled — use dependency injection. |
| Test setup huge | Extract helpers; still complex → simplify the design. |

## Debugging Integration

Found a bug? Write a failing test reproducing it, then follow the cycle. Never fix a bug without a test.

A known bug you can't fix in the same quest ships as a committed repro marked `@pytest.mark.xfail(strict=True, reason="...")`. Master stays green; when the fix lands the test xpasses, strict mode flips that into a failure, and the repro auto-converts into a regression guard.

## Testing Anti-Patterns

**Iron laws:** never test mock behavior; never add test-only methods to production classes; never mock without understanding dependencies.

**AP1 — testing mock behavior.** Asserting a component *is* its mock's return value (`assert result is mock.find_by_id.return_value`) tests the mock, not the code. Fix: assert the real returned value (`assert result == {'id': 1, 'name': 'Alice'}`). Gate: before asserting on any mock element, ask "real behavior or just mock existence?" — if existence, delete the assertion or unmock.

**AP2 — test-only methods in production.** A method only tests use (e.g. `destroy()`) pollutes production and violates YAGNI/separation of concerns. Fix: move it to test utilities. Gate: before adding a method to a production class, ask "only used by tests?" (→ test utilities) and "does this class own this resource's lifecycle?" (if no, wrong class).

**AP3 — mocking without understanding.** Mocking a method whose side effect the test depends on breaks the test silently (mock away the config write and a duplicate-detection test can never fire). Fix: mock at the correct lower level (the slow/external operation), not the high-level method the test needs. Gate: before mocking, ask what side effects the real method has and whether the test depends on them; if unsure, run with the real implementation first, then add minimal mocking.

**AP4 — incomplete mocks.** A partial mock with only the fields your immediate test uses fails silently downstream when code reads an omitted field. Fix: mock the COMPLETE data structure as it exists in reality. Gate: before creating a mock response, check the real schema and include every field the system might consume.

**AP5 — integration tests as afterthought.** "Implementation complete, tests to follow" is wrong — testing is part of implementation. Gate: you can't claim complete without tests.

**AP6 — tautological / plan-echo tests.** An `expected` copied from the code under test verifies transcription, not behavior, and stays green even when the feature is broken (revert the impl, return a wrong value — a plan-echo test still passes). Fix: derive `expected` from the requirement so it encodes an observable consequence a broken impl can't fake. Gate: before committing a test, ask "if I broke the feature, would THIS test fail?" — if no, it pins transcription; rewrite it. `expected` is justified by the requirement, never copied from the implementation.

### When mocks get too complex

Setup longer than the test, mocking everything, mocks missing methods the real components have, tests that break when the mock changes — prefer integration tests with real components.

**Mocks are tools to isolate, not things to test.** If TDD reveals you're testing mock behavior, you've gone wrong: test real behavior, or question why you're mocking at all.

## Final Rule

```
Production code → a test exists and failed first
Otherwise → not TDD
```

No exceptions without the user's permission.
