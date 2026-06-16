---
description: Find and remove dead code (unreachable branches, unused functions/exports, orphaned files) and commit the removal as one change.
---

# Dead-code sweep playbook

You are removing dead code from this project as a single, reviewed change. This
is a code/artifact pass: read and edit the repository directly — do not drive the
app or any live surface.

{% if args %}**Scope from the caller:** {{ args }}

Limit the sweep to that scope.{% else %}Sweep the whole project unless a later instruction narrows the scope.{% endif %}

## What counts as dead code

- Functions, classes, methods, and module-level names defined but never referenced.
- Unreachable branches (conditions that can never be true, code after an
  unconditional return/raise, `if False:` blocks).
- Unused imports and re-exports kept only for backward compatibility.
- Orphaned files and assets nothing imports or loads.

## How to work

1. Enumerate candidates with the project's own tooling: grep for definitions and
   cross-reference call sites, and use any dead-code linter the repo configures.
2. For each candidate, confirm it is truly unreferenced — check dynamic dispatch,
   string-based lookups, test-only usage, and public-API surface before deleting.
3. Remove confirmed dead code. Do not refactor surrounding code or add features.
4. Run the project's own test, lint, and typecheck tooling and make it green.
5. Commit the removal with a clear message.

Report what you removed and why each was safe to delete.
