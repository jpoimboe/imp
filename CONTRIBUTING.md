# Contributing to imp

## Sign off your commits

imp uses the [Developer Certificate of Origin](DCO). Every commit must carry a
`Signed-off-by` line certifying that you wrote the patch, or otherwise have the
right to submit it under the project's licence:

    Signed-off-by: Your Name <your@email>

`git commit -s` adds it for you. Use your real name and an address you can be
reached at. There is nothing else to sign and no agreement to file — the
sign-off *is* the certification, and its meaning is the text in [DCO](DCO).

You keep the copyright in what you write. Contributions are made under Apache
2.0, the project's licence, per section 5 of [LICENSE](LICENSE).

## Development

Setup and layout are in the [README](README.md). The pre-merge gate is:

    make check

which runs the dependency check, ruff, mypy, the lockfile check, and the test
suite. It must pass before a pull request is reviewed.

New behaviour is developed test-first: write the test, watch it fail for the
right reason, then write the smallest code that passes it.

## Reporting bugs

Open an issue with the imp version, what you expected, what happened, and the
smallest reproduction you can manage. If a quest was involved, the output of
`imp log <id>` is the single most useful thing you can attach.
