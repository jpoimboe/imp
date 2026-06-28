**Delegate exploration; read inline only what you will edit.** Map unfamiliar code
by dispatching `explorer` subagents — issue several
`sub_agent(subagent_name="explorer", …)` calls in a single message to run them in
parallel — rather than reading file after file inline. Use `researcher` when a
question turns on an external fact (does a library exist, what is current best
practice, how does an API behave). Reserve inline `read_file` for the few files you
will actively edit; inline `bash`/`grep` stays fine for quick checks. Pushing deep
mapping out to the subagents keeps your own context lean.
