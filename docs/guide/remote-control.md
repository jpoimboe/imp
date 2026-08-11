# Remote control

How to drive an `imp new` run from a chat surface: choosing a driver, supplying its credentials, and running the per-repo daemon that keeps the chat connected.

imp can stream a run's events to an external chat surface and accept replies back —
so you can watch and drive `imp new` from your phone. Enable exactly one built-in
driver by name in your machine's `~/.imp/config.yaml`:

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

## Telegram setup

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

4. Start the per-repo remote-control daemon and leave it running:

   ```bash
   imp remote-control start        # keeps this repo's chat connected
   ```

   `start` runs in the foreground and holds the terminal, so keep that terminal open for
   as long as you want the chat connected. To stop the daemon, run the following from a
   separate terminal:

   ```bash
   imp remote-control stop
   ```

   This daemon is separate from `imp new` and must be started and kept running on its
   own. Until it is up, the connector waits silently and delivers nothing — no events
   reach your chat and your replies are not received, even with the credentials above
   configured correctly.

A run validates these once at startup (a `getMe` / `getChat` self-check). A rejected
token or unreachable chat is reported at **WARNING** with a fix hint — otherwise a
broken credential fails silently and no notifications arrive. The driver's other
diagnostics stay quiet unless you enable debug logging for the `imp.remote` logger.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
