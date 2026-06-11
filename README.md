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
  #   reply_timeout: null       # wait forever, like a local prompt
```

Credentials stay in the environment (never in config). Built-ins:

| Driver | Description | Required env |
|--------|-------------|--------------|
| `telegram` | Drive an imp run from a Telegram chat (outbound getUpdates; stdlib-only). | `IMP_TELEGRAM_TOKEN`, `IMP_TELEGRAM_CHAT_ID` |

Run `imp config` to see the available drivers and which one is enabled.
