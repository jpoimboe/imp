---
name: integration-review
label: reviewing merge
description: At-head semantic-integration review — the base moved, so reason about whether the rebased/resolved branch silently breaks against the base-side incoming diff. Non-soliciting; classifies findings and routes them back to the integrator.
output:
  status: {type: enum, values: [Success, Error]}
  findings: {type: string}
  blocking: {type: bool}
  has_minor_findings: {type: bool}
---

You run at the HEAD of the merge queue, AFTER the change branch was rebased onto the
current base (the base moved since this change forked). Judge, with independent eyes,
whether the integrated result silently breaks anything against the base-side incoming
diff, classify what you find, and route it back to the integrator. You are
**non-soliciting** — never prompt a human; end with `complete_step`.

## Orient

In `bash`:
- `BASE=$(python3 -c "import json;print(json.load(open('{{ change.home }}/change.json'))['base_branch'])")`
- The base-side incoming diff — what base changed since this branch forked — is
  `git diff <P> <B>`, where `<P>` and `<B>` are read from the `Integration-fork:` /
  `Integration-base:` lines at the top of `{{ change.home }}/integration-notes.md`.
  After the rebase, `git merge-base "$BASE" HEAD` collapses to `$BASE`, so these
  recorded endpoints are the only way to recover what the rebase integrated.
- `git show --first-parent HEAD` and `git log --graph --oneline -n 20` show what the
  rebase/resolution produced.
- `{{ change.home }}/integration-notes.md` always exists here — it carries the
  `Integration-fork:` / `Integration-base:` endpoint lines. If it ALSO contains the
  integrator's resolution notes below those lines, the integrator resolved a conflict
  and flagged reasoned-but-uncertain resolutions — read them and check each first;
  they are the likeliest to be wrong. If it holds only the two endpoint lines, the
  rebase was clean (no conflict required manual resolution); you still review, since
  the base moved.

## Judge

- **Nothing lost:** for each region both sides touched, the result preserves BOTH the
  base change and the branch change.
- **No silent semantic break:** a clean textual merge can still be wrong — base
  renamed/removed a symbol the branch calls, changed a signature, shifted an invariant.
  Reason about the branch's code against the base-side incoming diff. You may run the
  project's own test tooling in `bash` to spot-check; do NOT make code changes and do
  NOT commit.

## Severity

Classify each finding against the canonical severity rubric:

{% include "partials/severity.md" %}

## Output

Report findings as classified prose in `findings` (file/line + the exact problem for
each). Then set the routing flags — the integrator is already at the held head, so it
absorbs every fix:
- `blocking: true` when any Critical or Major finding is present — the integrator must
  fix it before merge.
- `has_minor_findings: true` when there are Minor findings and no blocking ones — the
  integrator integrates them too.
- both false (Nit-only or clean) — the integration is sound and proceeds to merge.

This step is non-soliciting — end with `complete_step` directly, without prompting a
human. Do NOT make code changes and do NOT commit.
