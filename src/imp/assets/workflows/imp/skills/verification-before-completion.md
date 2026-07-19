# Verification Before Completion

Claiming work complete without verification is dishonesty, not efficiency — evidence before claims, always. Violating the letter violates the spirit: dodging the exact phrase does not dodge the rule. It binds exact phrases, paraphrases, synonyms, implications of success, and ANY communication suggesting completion or correctness.

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

Not run the verification command this step? You cannot claim it passes. Calling `complete_step` with success IS a completion claim — run the command and confirm its output before marking done.

## The Gate Function

Before claiming any status or calling `complete_step` with success:

```
1. IDENTIFY the command that proves the claim
2. RUN it full, fresh, complete via bash
3. READ the output — exit code, count failures
4. VERIFY it confirms the claim:
     NO  → state actual status + evidence
     YES → state the claim + evidence
5. ONLY THEN claim, or call complete_step
Skip any step = lying, not verifying
```

## Evidence Required

| Claim | Sufficient | NOT sufficient |
|-------|-----------|----------------|
| Tests pass | Test output: 0 failures | Previous run, "should pass" |
| Linter clean | Linter output: 0 errors | Partial check, extrapolation |
| Build succeeds | Build command: exit 0 | Linter passing (doesn't check compilation), logs "look good" |
| Bug fixed | Original symptom re-tested: passes | Code changed, assumed fixed |
| Regression test works | Verified red-green cycle | Passes once |
| Agent completed | VCS diff shows the changes | Agent's "success" report |
| Requirements met | Line-by-line checklist | Tests passing |

## Patterns

- **Regression (TDD red-green):** ✅ write → run (pass) → revert fix → run (MUST FAIL) → restore → run (pass). ❌ "I've written a regression test" without the cycle.
- **Requirements:** ✅ re-read plan → build checklist → verify each item → report gaps or completion. ❌ "Tests pass, phase complete".
- **Agent delegation:** ✅ report → check VCS diff → verify changes → report actual state. ❌ trust the report.

## Red Flags — STOP

- Hedges: "should", "probably", "seems to"
- Premature satisfaction: "Great!", "Perfect!", "Done!"
- About to call `complete_step`/`commit` unverified
- Trusting agent success reports
- Relying on partial verification
- "just this once"
- Tired and wanting the work over
- **ANY wording implying success without having run verification**

## Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'm confident" | Confidence ≠ evidence |
| "Just this once" | No exceptions |
| "I'm tired" | Exhaustion ≠ excuse |
| "Partial check is enough" | Partial proves nothing |
| "Different words, rule doesn't apply" | Spirit over letter |

## Why It Matters

Unverified completion ships undefined functions (crash) and missing requirements (incomplete features), and burns time on false completion → redirect → rework.

## Always Verify Before

Any success/completion claim, any satisfaction, any positive statement about work state, calling `commit`/`complete_step` or starting PR creation, moving to the next task, delegating to agents.

## Bottom Line

No shortcuts: run the command via `bash`, read the output, THEN claim the result or call `complete_step`. Non-negotiable.
