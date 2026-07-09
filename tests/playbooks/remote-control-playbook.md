# Remote-control chat playbook — multi-Change chat e2e (fake Telegram harness)

> **You are the test driver.** This file is your full instruction set. You drive a complete
> Change **from the chat** — through the fake Telegram harness against a real
> `imp remote-control start` daemon — to completion + merge, then exercise `/list`, a second
> `/new` + `/abandon`, and the unaddressed-message hint, judge whether the multi-Change chat UX
> reads well on a phone, and write a PASS/FAIL + UX verdict report. Work in a throwaway standalone
> `git clone` and abandon everything at the end.
>
> Read the whole file before you touch a tool. The throwaway-clone rationale and the fake-Telegram
> harness reference live once in `tests/playbooks/README.md` (the **Throwaway-clone reference** and
> **Shared fake-Telegram harness reference** sections); this playbook cites them rather than
> repeating them.
>
> **Cost & cadence.** A full live multi-agent run is ~25 min and **billable** (or a local
> `claude_code` session). Run it on demand. An environmental stall (provider quota/drift, a sandbox
> hiccup) is not a product defect — re-run or note it, do not fail the test.

---

## 1. Setup — throwaway clone, config, fake harness, and the seeded intent

Run in a **standalone `git clone`** (not a `git worktree add`) — see the README's
**Throwaway-clone reference** for why.

```bash
REPO=$(git rev-parse --show-toplevel)        # main checkout — the report lands here
cd "$REPO"
WT=/tmp/imp-remote-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"
ls "$WT"/imp/project.yaml                     # confirms $WT is an imp project
```

Force a single provider via the gitignored **local** layer (default `claude_code`; substitute
another only if the operator names one):

```bash
mkdir -p "$WT/.imp"
cat > "$WT/.imp/config.yaml" <<'YAML'
providers: [claude_code]   # exactly one of: claude_code | anthropic | openai | codex
theme: ansi
remote:
  driver:
    name: telegram
YAML
```

Start the **fake Telegram harness** and point the daemon at it. Allowlist a chat id and a fake
token; the harness ignores the token contents:

```bash
export IMP_TELEGRAM_CHAT_ID=4242
python3 -m tests.playbooks.fake_telegram > /tmp/fake-url.txt &   # prints its URL on the first stdout line
FAKE_PID=$!
until [ -s /tmp/fake-url.txt ]; do sleep 0.1; done               # wait for the URL line to land
FAKE_URL=$(head -1 /tmp/fake-url.txt)
echo "fake at $FAKE_URL"                                         # e.g. http://127.0.0.1:PORT
```

> **The harness prints its URL on its first stdout line.** Backgrounding hides that line, so
> redirect stdout to a file and read it back (above); never `curl` a placeholder address.

Start the daemon pointed at the fake API base, with the fake token + allowlisted chat:

```bash
cd "$WT"
IMP_TELEGRAM_API_BASE="$FAKE_URL" IMP_TELEGRAM_TOKEN=TOK IMP_TELEGRAM_CHAT_ID=4242 \
  imp remote-control start &
DAEMON_PID=$!
sleep 2
```

**Seed a deterministic feature intent carrying a unique sentinel:**

```bash
SENTINEL="IMP_REMOTE_$(openssl rand -hex 6)"
echo "$SENTINEL"
```

The intent you will type from the chat (§2) is exactly this with `$SENTINEL` substituted:

> Add a new function `remote_marker()` to a suitable module under `src/` that returns the exact
> string `"<SENTINEL>"`. Keep the change minimal and self-contained — one small function, no
> unrelated edits.

---

## 2. Drive a full Change from the chat to completion + merge

Drive entirely through the harness's `/_inject` (the phone typing) and `/_outbox` (the phone
screen). Helpers:

```bash
inject() { curl -s -X POST "$FAKE_URL/_inject" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$1")"; echo; }
inject_reply() { curl -s -X POST "$FAKE_URL/_inject" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1], "reply_to_message_id": int(sys.argv[2])}))' "$1" "$2")"; echo; }
outbox() { curl -s "$FAKE_URL/_outbox" | python3 -m json.tool; }
```

1. **`/new`** the seeded intent: `inject "/new <the §1 intent text with the sentinel>"`. Watch
   `outbox` for the optimistic "Spawning a new Change…" ack, then for the spawned Change's first
   narration arriving as a plain message — narrative events carry no reply footer; the `↩ @<handle>`
   footer appears only on the awaiting-user gate you answer later.
2. **Narration streams** to the chat as the run proceeds (spec → plan → implement → review). The
   spec phase has no confirmation gate, so the run flows from spec into planning automatically; the
   first human gate you answer remotely is the **merge-approval** gate at `finalize`.
3. **Answer the merge-approval gate** by replying to that question's message: read its `message_id`
   from `outbox`, then `inject_reply "approve" <message_id>`. (A reply-to threads to the Change;
   `@<handle> approve` also works.) Run `outbox` again and confirm the Change proceeds toward merge,
   verifying the reply was routed correctly.
4. Drive to **completion + merge**. Confirm: `cd "$WT" && imp list --all` shows the change
   **completed** and the working tree is clean.

**Resilience (environmental, not product).** If the run dies for an environmental reason, re-run or
re-interpret rather than failing the test; record it as an environmental note.

---

## 3. Exercise the rest of the grammar

With the first Change merged:

1. **`/help`** — `inject "/help"`; confirm `outbox` shows the help text listing the available
   commands (`/list`, `/new`, `/abandon`, `/help`). Also try an unknown command (`inject "/frobnicate"`)
   and confirm `outbox` shows the "Unknown command. Try /help." notice.
2. **`/list`** — `inject "/list"`; confirm `outbox` shows one line per Change in the
   `@<handle>  <title> — <status> · <step>` form, readable at a glance on a phone.
3. **Second `/new` + `/abandon`** — `inject "/new add a throwaway README note"`; once its first
   message appears, `inject_reply "/abandon" <message_id>` (or `inject "/abandon @<handle>"`);
   confirm the "Abandoning @<handle>…" ack and that `imp list --all` no longer lists it as live.
4. **Unaddressed-message hint** — `inject "hello?"` with no reply-to and no `@handle`; confirm
   `outbox` shows the route hint ("Couldn't route that — reply to a Change's message, prefix
   @handle, or send /list."), not a silent drop.

---

## 4. Verify the merged work

Run the provider-free checker against the clone, passing the sentinel from §1:

```bash
python3 "$REPO"/tests/playbooks/check_dogfood_invariants.py "$WT" "$SENTINEL"
echo "checker-exit=$?"
```

Fold the checker's `PASS`/`FAIL` lines and its `RESULT:` line verbatim into the report as fact.

---

## 5. Judge the chat UX (fresh eyes)

Judge whether the multi-Change chat experience reads well on a phone:

- **Handles & threading** — does each Change's `@<handle>` stay stable, and do reply-to threads land
  on the right Change?
- **`/list` formatting** — is the one-line-per-Change form scannable?
- **Acks** — are the optimistic "Spawning…" / "Abandoning…" acks reassuring without being noisy?
- **Hints** — is the unroutable-message hint actually helpful?

Emit a single critical line — `REMOTE-CONTROL: PASS` or `REMOTE-CONTROL: FAIL` — plus per-category
notes that justify it.

---

## 6. Output — the results report

Write one report to the **main checkout** (survives clone deletion):

```
"$REPO"/docs/bugs/<YYYY-MM-DD>-remote-control-playbook-results.md
```

It opens, top to bottom, with: (1) the critical `REMOTE-CONTROL: PASS/FAIL` line, the build under
test (`git -C "$REPO" rev-parse --short HEAD`), and the provider + sentinel; (2) the checker's
verbatim `PASS`/`FAIL` summary and `RESULT:` line; (3) the §3 grammar observations and the §5 UX
findings, each as a short, reproducible note. **A clean run still writes a report** — `PASS`, the
all-green invariant summary, and an explicit "no defects observed" note.

---

## 7. Cleanup

```bash
kill "$DAEMON_PID" "$FAKE_PID" 2>/dev/null
cd "$WT" && imp list | grep . && imp abandon <id>   # skip if no open change
rm -rf "$WT"
git -C "$REPO" status --short   # only the new report file should appear
```

Stop with a one-paragraph summary: build tested, provider, the `REMOTE-CONTROL: PASS/FAIL` verdict,
the invariant `RESULT:` line, headline UX findings (or "none"), and the report path.
