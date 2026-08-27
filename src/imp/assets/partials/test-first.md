**Test-first.** No production code without a failing test first — wrote the code
before its test? Delete it and implement fresh from the test. Write one minimal
test for one behavior, give it a name that states that behavior, and run it with
the project's own test tooling. Confirm it **fails because the feature is
missing** — not from a typo, and not passing already (if it passes you are
testing existing behavior; fix the test). Then write the simplest code that
passes it. Refactor only once green, and add no behavior while refactoring.

Three checks the cycle itself does not make:

- **Would this test fail if I broke the feature?** If not it pins transcription,
  not behavior: a plan-echo test whose expected value was copied from the code
  stays green when you revert the implementation and return something wrong.
  Derive the expected value from the requirement, never from the code you just
  wrote.
- **Does every mock carry the fields the code reads?** A partial mock holding
  only the fields your assertion touches passes here and fails silently
  downstream the moment something reads an omitted one. Mock the complete
  structure as it exists in reality.
- **Is exactly one assertion the oracle?** An acceptance test has one assertion
  that proves the feature works — assert that one strictly. Corroborating counts
  that vary run to run (retries, concurrent workers, generated ids) take bounds
  (`>= N`), never exact equality. Relaxing the whole test to stop a flake
  destroys the oracle along with it.

A bug you cannot fix in this quest ships as a committed repro that the suite
records as an **expected failure**: the branch stays green while the bug is open,
and when the fix lands the repro starts passing, the runner turns that unexpected
pass into a failure, and the repro becomes a regression guard. In pytest that is
`@pytest.mark.xfail(strict=True, reason="…")`; use your runner's equivalent.
