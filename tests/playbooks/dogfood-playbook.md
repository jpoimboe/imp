# imp dogfood playbook — outcome/e2e acceptance test (impty MCP)

> **You are the test driver.** This file is your full instruction set. You drive a complete
> `imp new` run *through the impty MCP server* (`mcp__impty__start` / `send` / `send_signal` /
> `view` / `wait` / `snapshot` / `stop`) — a real pty with a faithful pyte render — to
> **completion + merge**, judge the produced work with fresh eyes, run the invariant checker, and
> write a critical PASS/FAIL results report. This is the **outcome** lens: *did the autonomous
> pipeline produce correct, clean, merge-ready work?* (not the polish playbook's UX lens). Work in
> a throwaway standalone `git clone` and abandon everything at the end.
>
> Read the whole file before you touch a tool. The impty fidelity caveats and the
> throwaway-clone rationale live once in `tests/playbooks/README.md` (the **Shared impty fidelity reference**
> and **Throwaway-clone reference** sections); this playbook cites them rather than
> repeating them, and restates only its own driving primitives inline.
>
> **Cost & cadence.** A full live multi-agent run is ~25 min and **billable** (or a local
> `claude_code` session). Run it on demand. If a run looks stuck/looping for minutes with no
> progress, `stop` it and re-run — an environmental stall is not a product defect (§2).

---

## 1. Setup — throwaway clone, config, and the seeded feature intent

Run in a **standalone `git clone`**, not a `git worktree add` — see the README's
**Throwaway-clone reference** for why (a clone's control root is *itself*, so the local-layer
provider override both wins and stays isolated; reports written to the main checkout survive the
clone's deletion).

```bash
REPO=$(git rev-parse --show-toplevel)        # main checkout — the report lands here
cd "$REPO"
WT=/tmp/imp-dogfood-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"                       # standalone clone of HEAD; control root resolves to $WT
ls "$WT"/imp/project.yaml                     # confirms $WT is an imp project
```

Force a single provider via the gitignored **local** layer (default `claude_code`; substitute
another only if the operator names one):

```bash
mkdir -p "$WT/.imp"
cat > "$WT/.imp/config.yaml" <<'YAML'
providers: [claude_code]   # exactly one of: claude_code | anthropic | openai | codex
theme: ansi
YAML
cd "$WT" && imp config      # the `providers` row must resolve to your provider, ending in
                            # `…/.imp/config.yaml (local)` — NOT `(user)`. If it ends in `(user)`,
                            # your file landed at the wrong path; fix it before driving.
```

**Seed a deterministic feature intent that requests a unique sentinel.** Generate the sentinel and
keep it in a shell var so §4 can pass it to the checker:

```bash
SENTINEL="IMP_DOGFOOD_$(openssl rand -hex 6)"   # form: IMP_DOGFOOD_<random-hex>
echo "$SENTINEL"
```

The intent you will type at imp's opening prompt (§2) is exactly this, with `$SENTINEL`
substituted to its literal value — a small, observable, deterministic edit in `src/`:

> Add a new function `dogfood_marker()` to a suitable module under `src/` that returns the exact
> string `"<SENTINEL>"`. Keep the change minimal and self-contained — one small function, no
> unrelated edits.

The sentinel is **location-agnostic**: you do not dictate which file. The invariant checker proves
it landed *somewhere* in committed `src/`; the judging in §3 decides whether the placement is sane.

---

## 2. Driving — CLI profile, run to completion + merge

Restatement of the **CLI driving profile** (cite the README's **Shared impty fidelity reference** for the keystroke-vs-
signal caveats; the only ones that bite here are at prompts and on a deliberate pause):

- **Direct spawn**, cwd inside the clone, `exec` so imp (not bash) leads the foreground group:

  ```
  mcp__impty__start(argv=["bash","-lc",
    "export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key 2>/dev/null); export OPENAI_API_KEY=$(cat ~/.openai-key 2>/dev/null); exec imp new"],
    cwd="<WT>", cols=100, rows=30)
  ```
- **Answer in-turn prompts with `send`.** When the run parks at a prompt (opening intent, a
  workflow gate, an agent asking you mid-turn), type the answer and submit with a trailing `\r`,
  e.g. `send("<the §1 intent text>\r")`. Read state with `view()` / `view(full=True)` /
  `snapshot()`; `view()`'s header `alive=…` flips to `False` when imp exits.
- **Drive resumes.** If the run pauses (or you must pause it), continue it with
  `mcp__impty__start(argv=["bash","-lc","… exec imp resume <id>"], cwd="<WT>", …)`. Get `<id>`
  from `bash: cd "$WT" && imp ls` (its first column is the change id).
- **`send_signal(sig=…)` only where a deterministic pause is wanted** (SIGINT=2, SIGTERM=15) — a
  mid-run cooked-mode Ctrl-C keystroke is unreliable per the README fidelity reference. You do not
  normally need it: dogfood drives the run **forward to completion + merge**, not to an interrupt.

Drive the full pipeline — spec → plan → implement → review → **merge** — answering prompts as a
reasonable operator would and accepting the workflow's gates so the change reaches a clean merge.
Confirm completion: `bash: cd "$WT" && imp ls --all` shows the change as **completed**, and the
working tree is clean.

**Resilience (environmental, not product).** If the run dies for an environmental reason — provider
quota/drift, a sandbox hiccup, a transport drop, a stall with no forward progress — **re-run or
re-interpret rather than failing the test.** Record it as an environmental note in the report, not
a defect. Only behavior the pipeline itself produced (a wrong/oversized diff, leaked internals, a
dirty tree, a failed merge) is a dogfood finding.

---

## 3. Judge the outcome — fresh eyes against the seeded categories

With the run merged, read the produced diff (`bash: cd "$WT" && git log --oneline -15` and
`git show` the change's commits) and judge it cold against these **seeded guideline categories** so
verdicts are comparable across runs:

1. **Request actually implemented** — a `dogfood_marker()` (or equivalently named) function exists
   in committed `src/` and returns the exact sentinel; the requested behavior is really there, not
   faked or stubbed.
2. **Scope tight** — no unrequested changes: the diff is the one small function plus its minimal
   surroundings, not drive-by refactors, reformatting, or unrelated edits.
3. **No leaked internals** — no engine state, scratch files, debug prints, or `.imp/` artifacts in
   the committed diff; nothing a careful author would be embarrassed to merge.
4. **Clean merge** — the change merged to the base branch (a `--no-ff` merge commit is present), the
   working tree is clean, and `imp ls --all` reads it back as completed.
5. **Rework / replanning noted as findings** — if the run looped (a failing run-tests resume, a
   reviewer goto, a task-lead needs-replan that triggers a supercycle, re-planning), **note it as a finding** with what triggered
   it. It is *not* an automatic FAIL — only a finding that informs the verdict.

The verdict is **critical**: a single binary `DOGFOOD: PASS` / `DOGFOOD: FAIL` for the run, plus the
per-category notes that justify it.

---

## 4. Invariant check — run the standalone checker

Run the provider-free checker against the clone, passing the sentinel from §1. It runs from the
**main checkout** so it works regardless of the clone's contents:

```bash
python3 "$REPO"/tests/playbooks/check_dogfood_invariants.py "$WT" "$SENTINEL"
echo "checker-exit=$?"
```

It asserts the kept mechanical invariants (clean working tree; no `.imp/` state in the run's git
history; an atomic non-merge commit touching the sentinel-bearing implementation file; spec and
plan artifacts committed by the run; `imp log` / `imp log --full` / `imp ls --all` read back the
completed change; the sentinel present anywhere in committed `src/`) and prints a per-check
`PASS`/`FAIL` summary ending in `RESULT: PASS (7/7)` (exit 0) or `RESULT: FAIL (k/7)` (exit 1). The
run's commit set defaults to `origin/HEAD..HEAD` (the clone point) — correct for a clone. **Fold
the checker's pass/fail lines verbatim into the report as fact** (§5); a checker FAIL is reported,
not judged away.

---

## 5. Output — the results report

Write one report to the **main checkout** (survives clone deletion):

```
"$REPO"/docs/bugs/<YYYY-MM-DD>-dogfood-playbook-results.md
```

It opens, top to bottom, with:

1. **The critical line first:** `DOGFOOD: PASS` or `DOGFOOD: FAIL` (the §3 verdict), plus the build
   under test (`git -C "$REPO" rev-parse --short HEAD`), the provider, and the sentinel used.
2. **The invariant results:** the checker's full `PASS`/`FAIL` summary and its `RESULT:` line
   (§4), verbatim.
3. **The findings:** the §3 per-category notes, any rework/replanning observed, and any
   environmental re-runs (§2) — each as a short, reproducible finding.

**A clean run still writes a report** — write the `DOGFOOD: PASS`, the all-green invariant summary,
and an explicit "no defects observed" note. A passing run that produces no artifact is the most
likely failure of this convention.

---

## 6. Cleanup

```bash
cd "$WT" && imp ls | grep . && imp abandon <id>   # skip if no open change (id from `imp ls`)
rm -rf "$WT"
git -C "$REPO" status --short   # only the new report file should appear
```

If anything in the main checkout differs beyond the report file, flag it. Stop with a one-paragraph
summary: build tested, provider, the `DOGFOOD: PASS/FAIL` verdict, the invariant `RESULT:` line,
headline findings (or "none"), and the report path.
