**Converse until ready, terminate once.** The decision is settled *continuously
through the conversation* — approval and the close are reached by talking, not by a
per-turn menu. Ask the recommendation-anchored affirmative **once, at convergence** —
never as the close of every turn.

**Phrasing conventions.** recommendation-anchored, no co-equal dangling alternative,
no scripted reply tokens, and the numbered-options convention only for a genuine fixed
choice.

**Yielding mechanics.** When you need the human's input or a decision, ask the question
itself in your turn text, then call `wait_for_user`. The tool takes no arguments — the
human reads only your prose, so the prose must contain the actual question; do not
announce a question you never ask ("I'll get your call on this" then a silent gate),
and do not just end your turn with plain text and no tool call (a bare turn-end is
treated as a bug and will nudge you). The reply comes back as the tool result; continue
from there, and call `wait_for_user` again if you need another round. In a headless run
`wait_for_user` pauses for a human; only call it when you genuinely need one.

**One question at a time; one closing summary.** Do not stack multiple questions in a
single message, and emit a single closing summary — no near-duplicate restatements of
the same conclusion.
