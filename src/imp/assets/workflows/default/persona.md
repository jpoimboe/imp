# The verify gates own the full suite

Never run the project's full test suite, lint, or typecheck. The `verify`,
`converge-verify` and `integration-verify` steps are mechanical gates that run the
project's combined checks and route failures back; a run of your own only duplicates
them, at real cost on a machine carrying other work. Run only the narrowest check
that tests a specific hypothesis you actually hold.
