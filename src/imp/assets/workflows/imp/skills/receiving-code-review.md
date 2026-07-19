# Code Review Reception

Code review is technical evaluation, not emotional performance. **Verify before implementing, ask before assuming, technical correctness over social comfort.**

## Role in imp

The `task-lead` adjudicates: it decides which reviewer findings are technically valid for this codebase and what to apply; the coder applies only accepted findings. This skill governs that evaluation — subject feedback to technical rigor, don't rubber-stamp it.

## The Response Pattern

```
1. READ: complete feedback without reacting
2. UNDERSTAND: restate the requirement in your own words (or ask)
3. VERIFY: check against codebase reality
4. EVALUATE: technically sound for THIS codebase?
5. RESPOND: technical acknowledgment or reasoned pushback
6. IMPLEMENT: one item at a time, test each
```

## Forbidden Responses

Never — performative, no technical content:
- ❌ "You're absolutely right!"
- ❌ "Great point!" / "Excellent feedback!"
- ❌ "Let me implement that now" (before verification)
- ❌ any gratitude — "Thanks for catching that!", "Thanks for [anything]"

Instead: restate the technical requirement, ask clarifying questions, push back with technical reasoning if wrong, or just start working. Actions > words — the code itself shows you heard the feedback.

## Handling Unclear Feedback

If ANY item is unclear, STOP — implement nothing yet, ask for clarification on the unclear items. Items may be related; partial understanding produces wrong implementation.

```
Reviewer: "Fix items 1-6", you understand 1,2,3,6, unclear on 4,5.
❌ implement 1,2,3,6 now, ask 4,5 later
✅ "Understand 1,2,3,6. Need clarification on 4 and 5 before implementing."
```

## Source-Specific Handling

**From the user** — trusted; implement after understanding. Still ask if scope is unclear. No performative agreement — skip to action or a technical acknowledgment.

**From external reviewers** — be skeptical, but check carefully. Before implementing, check:
- technically correct for THIS codebase?
- breaks existing functionality?
- is there a reason for the current implementation?
- works on all platforms/versions?
- does the reviewer understand the full context?

Then escalate:
- suggestion seems wrong → push back with technical reasoning
- can't easily verify → "I can't verify this without [X]. Should I [investigate/ask/proceed]?"
- conflicts with the user's prior decisions → stop and discuss with the user first

## YAGNI Check

When a reviewer suggests "implementing properly", grep the codebase for actual usage:
- unused → "This endpoint isn't called. Remove it (YAGNI)?"
- used → implement properly

You and the reviewer both report to the user; if the feature isn't needed, don't add it. **Scope:** YAGNI targets *speculative* unused features or abstractions, NOT a cheap defensive guard or assertion that hardens existing reachable code — that guard is cheap insurance, not a speculative feature.

```
Reviewer: "Implement proper metrics tracking (DB, date filters, CSV export)"
✅ "Grepped codebase — nothing calls this endpoint. Remove it (YAGNI)? Or is there usage I'm missing?"
```

## Implementation Order

```
Multi-item feedback:
1. Clarify anything unclear FIRST
2. Implement in order: blocking issues (breaks, security) → simple fixes (typos, imports) → complex fixes (refactoring, logic)
3. Test each fix individually
4. Verify no regressions
```

## When To Push Back

Push back when the suggestion: breaks existing functionality; comes from a reviewer lacking full context; violates YAGNI (unused feature); is technically incorrect for this stack; conflicts with legacy/compatibility constraints; conflicts with the user's architectural decisions.

How: technical reasoning not defensiveness; ask specific questions; reference working tests/code; involve the user if the matter is architectural.

```
Reviewer: "Remove legacy code"
✅ "Checking... build target is 10.15+, this API needs 13+ — need legacy for backward compat. Fix the wrong bundle ID or drop pre-13 support?"
```

## Acknowledging Correct Feedback

State the fix, no gratitude (see Forbidden Responses):
- ✅ "Fixed. [what changed]"
- ✅ "Good catch — [issue]. Fixed in [location]."
- ✅ just fix it and show it in the code

If you catch yourself about to write "Thanks", delete it and state the fix instead.

## Gracefully Correcting Your Pushback

If you pushed back and were wrong, state it factually:
- ✅ "You were right — I checked [X] and it does [Y]. Implementing now."
- ✅ "Verified this and you're correct. My initial understanding was wrong because [reason]. Fixing."

No long apology, no defending why you pushed back, no over-explaining — state the correction and move on.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Performative agreement | State requirement or just act |
| Blind implementation | Verify against codebase first |
| Batch without testing | One at a time, test each |
| Assuming reviewer is right | Check if it breaks things |
| Avoiding pushback | Technical correctness > comfort |
| Partial implementation | Clarify all items first |
| Can't verify, proceed anyway | State limitation, ask for direction |

## The Bottom Line

External feedback is suggestions to evaluate, not orders to follow — verify, question, then implement. No performative agreement; technical rigor always.
