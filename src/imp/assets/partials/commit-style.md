## Commit messages

Write the message to a file and `git commit -F "${TMPDIR:-/tmp}/msg"` — never
`-m`. A body has blank lines and punctuation no shell quoting survives.

**Subject:** `component: imperative summary` — no trailing period, under 100
characters. The component names the **part of the codebase that changed**, not the
kind of change: a commit that only adds tests for the worktree code is `worktree:`,
not `test:`. Reserve `test:` and `docs:` for changes to the test harness or the
documentation itself. Find the component the way a maintainer would — `git log
--oneline -30 -- <the paths you touched>` and reuse the prefix that area already
uses; with no precedent, use the module or directory name.

**Body:** wrapped at 75 columns, three movements, no headings:

1. **Context** — present tense, describing the deficient status quo. "The unwinder
   assumes the frame pointer is set up before the first push, which isn't true
   for ..."
2. **Consequence** — what actually goes wrong: the crash, the corruption, the
   user-visible symptom, the missed optimization. Include a trimmed traceback,
   reproducer, or measurement when one exists.
3. **The fix** — imperative mood. "Instead, defer the check until after ..." Never
   "This patch does X" or "This commit adds Y".

A body is required. It may be a single short paragraph when the change is
self-evident, but never absent.

**Voice:** impersonal — no "I", "we", "obviously", "trivially", "as you can see".
Refer to functions as `foo()` and to commits as `abcdef123456 ("subsys: subject")`,
never "the patch above" or "the previous commit" — commits get reordered. Never
reference a commit from this same quest: its hash changes when history is folded
and rebased.

**Self-contained.** A reader with only `git log` must understand it. Never mention
the plan, the task, the review, the spec, or imp itself.

Note anything user-visible: a changed CLI flag or output, a config key, a
performance regression, a behavior change that needs documentation.
