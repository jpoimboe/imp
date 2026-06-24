# imp introspection

How to answer a specific imp question from ground truth instead of memory. Each
procedure says what to read or run and how to turn the raw output into a
plain-language answer. Pair with `imp-mental-model` for the stable picture.

## What the current workflow is doing

Read the change's `workflow/workflow.yaml` (under the change home) for the ordered
phases and steps, then correlate it with the live step in `state/state.json` to
see where this change is right now. Turn it into plain language: name the phase
the user is in and what the next step does, without surfacing internal step ids.

## A CLI question

Run `imp help` for the command list, or `imp <cmd> --help` for one command's
flags and behavior. Read the help text back as the answer; quote the actual flag
names rather than guessing them.

## What work exists

- `imp list` — active in-flight changes plus the Backlog (including parked /
  `saved` changes).
- `imp list --all` — also includes completed and abandoned past changes.
- `imp search <query>` — search the archive (completed, abandoned, and left
  conversations) for changes whose title or id contains `<query>` (a required
  substring; bare `imp search` errors).

Summarize the rows in plain language (what is in flight, what is parked, what is
finished); do not invent changes that the commands did not list.

## Detail on one change

`imp show <id> [spec|plan|events|status|review]` — the subcommand selects the
facet: `spec` for the spec, `plan` for the plan(s), `events` for the timeline,
`status` for where it stands, `review` for review findings. Read the facet the
user asked about and answer from it.

## This change's own state

Read `state/state.json` in the current change home, or run
`imp show <id> status`, to report where this conversation's change stands.
