# imp

Project-local development lifecycle tool. A deterministic Python engine drives a declarative YAML workflow; the LLM is summoned per step with a fresh, tightly-scoped context.

See `docs/superpowers/specs/2026-05-24-imp-v0-design.md` for V0 design.

## Remote drivers

imp can stream a run's events to an external chat surface and accept replies back —
so you can watch and drive `imp new` from your phone. Enable exactly one built-in
driver by name in your machine's `~/.config/imp/config.yaml`:

```yaml
remote:
  driver: telegram          # bare name; or a mapping with knob overrides:
  # driver:
  #   name: telegram
  #   events: [all]             # or an explicit event-type allowlist
  #   startup_timeout: 10
```

Credentials stay in the environment (never in config). Built-ins:

| Driver | Description | Required env |
|--------|-------------|--------------|
| `telegram` | Drive an imp run from a Telegram chat (outbound getUpdates; stdlib-only). | `IMP_TELEGRAM_TOKEN`, `IMP_TELEGRAM_CHAT_ID` |

Run `imp config` to see the available drivers and which one is enabled.

### Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token. A bot
   token has the form `<id>:<auth>` — exactly **one** colon. Pasting any extra
   suffix makes every API call fail.
2. Open your new bot in Telegram and send it `/start` (or any message). A bot can
   only message a chat that has messaged it first.
3. Find your numeric chat id (e.g. message [@userinfobot](https://t.me/userinfobot)),
   then export both credentials:

   ```bash
   export IMP_TELEGRAM_TOKEN=123456789:AA...      # <id>:<auth>, one colon
   export IMP_TELEGRAM_CHAT_ID=987654321          # your numeric chat id
   ```

A run validates these once at startup (a `getMe` / `getChat` self-check). A rejected
token or unreachable chat is reported at **WARNING** with a fix hint — otherwise a
broken credential fails silently and no notifications arrive. The driver's other
diagnostics stay quiet unless you enable debug logging for the `imp.remote` logger.
