```console
$ imp log samo71
> add a --json flag to imp list

  … 279 events elided …

• Settled, then. To read it back in one piece before I set it up:

  Goal — imp list gains a --json flag that emits the board as structured data
  for agents and scripts.

  Scope — the flag on the list parser in src/imp/cli/main.py; a JSON branch in
  _cmd_list; extraction of the shared row-gathering out of _overview_lines /
  _filtered_overview_lines so both formats read one source; updates to the
  imp-knowledge skill bullet, docs/guide/introspection.md, and the README
  command block.

  Decisions — flat array in board order, each entry zone-tagged active / backlog
  / archive; raw ISO timestamps rather than relative ages; title null when
  unnamed with the full opening prompt in intent; backlog entries carry status:
  "backlog" plus provenance and readiness; step present only on in-flight
  entries that have one; machine status values kept underscored; all existing
  filters compose unchanged; no matches emits [].

  Acceptance — the output parses, carries the right entry per zone with the
  fields above, stays free of ANSI escapes, composes with --all / --backlog /
  --since, returns [] with exit 0 on an empty board, and leaves today's text
  output byte-identical.

  Ready for me to set this up and start building? Declining sets it aside, and
  you can pick it back up later with imp resume samo71.

> ok

  … 2,397 events elided …

• I walked all twelve acceptance criteria clause by clause against the committed
  code across the eight commits. Coverage is complete — every clause lands on a
  production line, and the behavioral ones have pinning tests.

  The three outline plans each delivered their scope:

   • The gatherer extraction is real: _overview_rows at src/imp/cli/main.py:3451
     loads and zone-sorts rows keyed on dispositions + cutoff, with
     _overview_lines (3416) and _filtered_overview_lines (3475) reduced to
     rendering wrappers that kept their signatures. The five test files the spec
     named as direct callers are untouched by the diff, and _cmd_status is
     unmodified — so AC 10(a) and 10(b) hold.
   • The flag sits at src/imp/cli/main.py:4414, after --since and before
     --no-pager with the help text verbatim (AC 11). All three _cmd_list exits
     honor it: the PreconditionError branch at 2091, the unfiltered branch at
     2101 (after disposition resolution), and the filtered branch at 2118 —
     which is reached after the --since parse, so an invalid spec still exits 2
     with a clean stdout (AC 8).
   • The encoder at 3487–3522 emits exactly the seven always-present keys, gates
     step on r[2] not in ("", "-") mirroring _quest_json's gate at 2056, reads
     provenance from slot 1 and derives readiness from slot 2 without re-calling
     backlog_readiness, and forces intent=None for backlog (AC 2–4). status
     flows straight from the machine word — the merge blocked spacing only ever
     happens in quest_row, which the JSON branch never reaches, and
     src/imp/cli/store.py:470 confirms the row slot stays underscored (AC 5).

  A couple of clauses worth naming explicitly. AC 2(a)'s "untruncated" is
  structurally guaranteed: intents comes from HomeDigest.intent_title, which is
  raw _resolve_intent output (src/imp/cli/store.py:151), and
  tests/test_cli_list_json.py:170 pins it with a 92-character two-line prompt
  that the board's 70-column cap would have clipped and collapsed. AC 9's
  ANSI-freedom is pinned twice — once with color forced on, once with isatty
  forced true. The one edit to an existing test
  (tests/test_cli_trailing_blank.py) only adds json=False to a hand-built
  Namespace; its asserted output is byte-identical.

  Documentation carries the flag on all three surfaces with the verbatim text,
  and the two assertions went into the right tests — the skill ordering check
  reads the loaded skill body, the guide check reads docs/guide/introspection.md
  — so the confusion the spec warned about didn't happen.

  Both deferred.md entries are closed in code, not just marked:
  _board_entry_json strips the title and gates intent on that same stripped
  value (pinned by the title="  " case at tests/test_cli_list_json.py:119), and
  the emission path has its piped/--no-pager test at 319.

  … 459 events elided …

• The base branch hasn't moved since the fork point, and there are no open
  decision items, so this one merges automatically without a gate.

  What it does. imp list gains a --json flag that emits the quest board as a
  structured JSON array — one object per quest, in display order (in-flight →
  backlog → archive) — so agents and scripts can read board state without
  parsing the padded, colored, width-capped text table. The implementation first
  extracts a shared row-gathering function (_overview_rows, keyed on
  dispositions and cutoff) so the text and JSON paths draw from one source and
  cannot drift in selection or ordering, then layers the encoder and flag on
  top. Every existing filter composes with it; an empty result always emits []
  with exit 0 (including the path where no control root exists); a bad --since
  spec still exits 2 as a usage error. The JSON shape reuses the vocabulary imp
  show --json and imp events search --json already established — id, zone,
  title, intent, status, created, last_activity, plus step on active entries and
  provenance/readiness on backlog ones. The flag is documented in the
  imp-knowledge skill, the introspection guide, and the README, each surface
  pinned by the test that reads it.

  Verification. The combined checks pass. There was one round-trip on the way
  here: a line-length lint failure in tests/test_overview_dedup.py, introduced
  by the cleanup that deleted a dead _history_rows shim and retargeted its test
  callers onto the longer _history_by_disposition name. That was fixed forward
  in fb9c9656 and the suite came back green.

  Open items. None. All three recorded followups were resolved during the run
  rather than deferred — two spec-conformance items folded into their tasks, and
  the dead-shim deletion the diff review caught, fixed in 9e1bb977.

  Approving this now; the merge runs immediately. Total diff is 9 files,
  +641/−55, most of it new tests.

  1 call · complete_step

── integration phase ───

integration phase · merging · complete

Merged samo71 into master.
```
