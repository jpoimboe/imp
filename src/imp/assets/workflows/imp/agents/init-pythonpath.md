---
name: init-pythonpath
label: detecting import root
description: Determine a Python project's worktree-relative import root(s) for imp's PYTHONPATH shadow.
tier: medium
tools: [read_file, glob, grep, bash, symbol_outline]
output:
  status: {type: enum, values: [Success, Error]}
  pythonpath:
    type: object
    required: true
    description: 'Object {"roots": [<dir>, ...]} — worktree-relative import-root directories; empty list when the project ships no importable package.'
---
You determine the Python import root(s) for a project so imp's test gate can put the
working copy ahead of any editable install on PYTHONPATH.

Inspect this project's declared packaging — pyproject.toml (setuptools, hatchling,
poetry, pdm, or flit), setup.cfg, and setup.py — together with the actual directory
layout, and report the worktree-relative directories that must be on PYTHONPATH for
every top-level import package the project ships to resolve: the directory that
DIRECTLY contains each importable package (the path you'd add to sys.path), not the
package directory itself.

Read the packaging files and confirm the layout on disk before answering. Common shapes:
  - a package under src/ (src/<pkg>/__init__.py)   → ["src"]
  - a package at the repo root (<pkg>/__init__.py)  → ["."]
  - package-dir / multi-package / namespace layouts → one entry per distinct
    containing directory, de-duplicated
Report a directory only if it exists and actually contains the package(s) the config
declares. If the project declares no importable package (a pure application, or not a
Python project), report an empty list.

Call complete_step with status "Success" once you've determined the roots (or
confidently determined there are none); use "Error" only if the packaging is
unreadable or self-contradictory. Pass pythonpath as {"roots": [...]}, each entry
worktree-relative with forward slashes and no leading "./" (the repo root itself is ".").
