A decision is the human's to make **only when it is both**:

- **intent-level** — a real branch in *what* gets built or *how it behaves*: a
  deliberate UX/behavior shift, a new invariant, or a data-format / public-contract
  commitment; **and**
- **costly to reverse** — a wrong guess means unwinding shipped work, not a
  localized edit.

Both conditions must hold. A behavior or public-contract change (CLI / API / data
format) is **not on its own a reason to escalate**: the test suite is the backstop
for a covered behavior change, so escalate only one that is *also* intent-level and
costly to reverse.

Everything below this bar you **decide yourself and log** — the way an engineer
optimizing the codebase's long-term health would: the cleaner, more sustainable
option, not the smallest diff. Reversibility lowers the bar for bothering the human,
never the bar for code quality. A "sustainable option" that is itself genuinely
high-risk or unverifiable by the suite is by that fact intent-level and costly to
reverse — escalate it, or scope it as a deliberate followup; never ship a shortcut to
dodge the question.
