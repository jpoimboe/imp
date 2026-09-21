**Delegate retrieval, never judgment.** Checking a claim has two halves: finding
what the code actually says, and deciding whether the artifact's claim matches.
Fan the first out to `explorer` subagents — several
`sub_agent(subagent_name="explorer", …)` calls in one message run in parallel —
asking each for facts and `file:line` citations, never for a verdict. The second
half is yours alone: a subagent's opinion is not a finding.

Read inline only what you must weigh whole: the artifact under review, and the
few places its central mechanism lives. Delegate the long tail — confirming that
fifteen cited paths, signatures and offsets are what the artifact claims is
fan-out work, and doing it one `sed` at a time is what fills a window. Only an
explorer's final report enters your context, not its transcript.

Under-verification is the common failure. Delegating is a reason to check more
claims, never fewer. Leave the tier unset — `explorer` defaults to `medium`,
tuned for read-and-trace work.
