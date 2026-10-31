---
name: poweroff
description: Power off a bench device (or all of them) and release its boot pin
user-invocable: true
---

Usage: `/poweroff <device>` or `/poweroff all`

## Steps

1. Look the device up: `<repo>/tools/bench.py show <device>` (this skill lives
   at `<repo>/skills/poweroff/`; resolve symlinks with `readlink -f`). For
   `all`, take every name from `bench.py list`.
2. In parallel: `mcp__relay-panel__set_relay` its `power` relay `off` and its
   `boot` relay `off`.
3. Report what was switched.

Relays only. Never switch a relay whose panel name contains
`master_name_contains` (the bench rails), and do not stop `dfu-remote` on the
Pi; it is meant to stay running.
