# imp-telegram — reference remote driver

A dependency-free reference implementation of imp's stdio **driver contract**: it
lets you watch and drive one `imp new` from Telegram on your phone.

It reads NDJSON events from imp on **stdin**, curates each into a Telegram message,
long-polls `getUpdates` (**outbound only** — no inbound port, tunnel, or hosting),
and writes chat replies to **stdout** for imp's input-race to pick up.

## Configure imp

Add a `remote.driver` block to imp's config (e.g. `.imp/config.yaml`):

```yaml
remote:
  driver:
    command: [imp-telegram]        # or: [python3, contrib/drivers/telegram/imp_telegram.py]
    events: [all]                  # or an explicit allowlist of event types
    startup_timeout: 10
    reply_timeout: null            # wait forever, like a local prompt
```

## Credentials (never committed to the repo)

Provide the bot token and the **allowlisted** chat id via environment variables:

```bash
export IMP_TELEGRAM_TOKEN=123456:ABC-your-bot-token
export IMP_TELEGRAM_CHAT_ID=987654321
```

…or via an out-of-tree `KEY=VALUE` file referenced by `IMP_TELEGRAM_CONFIG_FILE`:

```
IMP_TELEGRAM_TOKEN=123456:ABC-your-bot-token
IMP_TELEGRAM_CHAT_ID=987654321
```

The driver **refuses to start without `IMP_TELEGRAM_CHAT_ID`**: a reply is injected
straight into the coding agent's input, so only the allowlisted chat may drive it.

## Curation (tune without touching imp)

| Class | Event types | Telegram behavior |
|-------|-------------|-------------------|
| loud  | `awaiting_user_input`, `errored`, `merge_blocked`, `completed` | notify |
| silent | `agent_output`, `llm_response` | message, notification suppressed |
| dropped | everything else (`tool_call`, `tool_result`, `thinking`, `step_context`, `workflow_paused`) | ignored |

## Tests

```bash
python3 -m pytest contrib/drivers/telegram -q
```

These mock all HTTP and **do not gate imp core**.
