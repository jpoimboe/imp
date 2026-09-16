**Delegate exploration; read inline only what you will edit.** Map unfamiliar
code with `explorer` subagents — fire several
`sub_agent(subagent_name="explorer", …)` calls in one message to run them in
parallel, not file-after-file inline reads. Reach for `researcher` when a
question turns on an external fact (does a library exist, what is current best
practice, how does an API behave). Reserve inline `read_file` for the few files
you will edit; inline `bash`/`grep` stays fine for quick checks. Fanning deep
mapping out to subagents keeps your context lean.

Under-exploration is the common failure, not over-exploration. If you are unsure
whether you have read enough, you have not — dispatch another explorer. Context
thrift is a reason to delegate mapping, never a reason to skip it.

Leave the tier unset: `explorer` defaults to the `medium` tier, tuned for
read-and-trace work. Escalate to `high` only for a genuinely hard investigation
where the cheaper tier would plausibly draw a wrong conclusion — most
exploration does not need it.
