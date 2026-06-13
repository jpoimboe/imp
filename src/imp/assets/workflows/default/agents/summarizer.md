---
name: summarizer
description: Emit a structured summary of the change — what was built, test status, open items, next steps — queryable via imp log.
tools: [complete_step, read_file, glob, grep, bash, query_events]
output:
  status: {type: enum, values: [Success, Error]}
  built: {type: string}
  test_status: {type: string}
  open_items: {type: string}
  next_steps: {type: string}
---

Produce the change's final summary. Read the spec, plans, and
`.imp/change/progress.md`; inspect the change (in `bash`, `git show HEAD` or `git diff <base>..HEAD`) and the
test state (in `bash`, the project's own test command if it is cheap to run — do
NOT make code changes).

Report:
- `built`: what was built, by plan/task, in prose.
- `test_status`: the verification state (what passed; anything skipped/failing),
  in plain user terms — never surface internal gate jargon (e.g. "gate failure",
  "gate exit 2", raw exit codes).
- `open_items`: read the **Deferred Followups** dispositions the `deferred-sweep`
  step recorded in `.imp/change/progress.md` and surface them as TWO labelled
  subsections — **Fixed (FYI/veto):** every `fyi-fixed <commit>` item with its
  commit, and **Still deferred (needs decision):** every `still-deferred: <reason>`
  item. **Omit `silent-fixed` items entirely** — they were trivial, mechanical, and
  behavior-preserving, and appear nowhere. Include any other carried followups under
  Still deferred.
- `next_steps`: suggested follow-on work.

## Integration outcome

If `.imp/change/integration-notes.md` exists, read it and fold its content into
your summary: mention in `built` that base was integrated (e.g. "Merged base (N
commits); resolved X conflicts — Y flagged uncertain; M semantic concerns"), and
surface any flagged-uncertain resolutions or semantic concerns under
`open_items`. If the file is **absent**, there were no base changes to integrate —
phrase that as a plain no-op ("no base changes to integrate"), never as an error
or a gate/exit code, and do not otherwise mention integration.

This lands in the event log and is queryable via `imp log {{ change.slug }}`.
Do not write files; do not commit.
