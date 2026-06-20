---
name: integration-review
label: reviewing merge
description: At-head semantic-integration review — the base moved, so reason about whether the rebased/resolved branch silently breaks against the base-side incoming diff. Non-soliciting; routes ok or breakage.
output:
  status: {type: enum, values: [Success, Error]}
  verdict: {type: enum, values: [ok, breakage]}
  summary: {type: string}
---

You run at the HEAD of the merge queue, AFTER the change branch was rebased onto
the current base (the base moved since this change forked). Your job is to judge,
with independent eyes, whether the integrated result silently breaks anything —
**without** asking a human (that is the bypass gate's job). Decide `ok` or
`breakage` and report.

## Orient

In `bash`:
- `BASE=$(python3 -c "import json;print(json.load(open('{{ change.home }}/change.json'))['base_branch'])")`
- The base-side incoming diff — what base changed since this branch forked — is
  `git diff <P> <B>`, where `<P>` and `<B>` are read from the `Integration-fork:` /
  `Integration-base:` lines at the top of `{{ change.home }}/integration-notes.md`.
  After the rebase, `git merge-base "$BASE" HEAD` collapses to `$BASE`, so these recorded
  endpoints are the only way to recover what the rebase integrated.
- `git show --first-parent HEAD` and `git log --graph --oneline -n 20` show what
  the rebase/resolution produced.
- `{{ change.home }}/integration-notes.md` always exists here — it carries the
  `Integration-fork:` / `Integration-base:` endpoint lines. If it ALSO contains the
  integrator's resolution notes below those lines, the integrator resolved a textual
  conflict and flagged reasoned-but-uncertain resolutions — read them and check each
  first; they are the likeliest to be wrong. If it holds only the two endpoint lines, the
  rebase was clean (no conflict required manual resolution); you still review, since the base moved.

## Judge

- **Nothing lost:** for each region both sides touched, the result preserves BOTH
  the base change and the branch change.
- **No silent semantic breakage:** a clean textual merge can still be wrong — base
  renamed/removed a symbol the branch calls, changed a signature, shifted an
  invariant. Reason about the branch's code against the base-side incoming diff.
  You may run the project's own test tooling in `bash` to spot-check; do NOT make
  code changes and do NOT commit.

## Output

- `verdict: ok` — the integration is sound; the change proceeds to verify and merge.
- `verdict: breakage` — the integration silently breaks something, or a flagged
  resolution is wrong. Name the file/line and the exact problem in `summary`; the
  change bypasses to a human resolution gate.
- `summary` — a short prose account: how many base commits were integrated, what
  you checked, and (on breakage) precisely what is wrong.

This step is non-soliciting — end with `complete_step` directly, without prompting a human.
