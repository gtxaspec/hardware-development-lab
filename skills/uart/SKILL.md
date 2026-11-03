---
name: uart
description: Start/stop/read UART consoles on the bench, rotate and save logs, break into U-Boot
user-invocable: true
---

Manage the bench's UART consoles through the uart MCP server. Supports logging,
log rotation across test cycles, live reads, and interactive
sessions (U-Boot, Linux shell).

Usage:
- `/uart start <device>` - start background UART capture
- `/uart stop <device>`  - stop capture
- `/uart read <device> [lines]` - last 40 lines (or N)
- `/uart status` - list all devices and capture state
- `/uart fresh <device> [tag]` - rotate the current log to a saved
  copy and truncate, so the next test cycle gets a clean log
  without losing the previous one
- `/uart save <device> <tag>` - snapshot the current log to a
  named copy WITHOUT truncating (keep streaming)
- `/uart uboot <bench device>` - power cycle and break into U-Boot
- `/uart send <device> <command>` - send a command, show response
- `/uart clear <device>` - clear the in-MCP read buffer (does NOT
  touch the on-disk log file - use `fresh` for that)

<device> = a port (5, ttyUSB5 or TCP 3005) or an optional alias. Aliases live in
`mcp/uart/aliases.json` in this repo; set one with `mcp__uart__name_uart` only
after seeing that board's boot banner on the port. `/uart status` lists ports
and aliases. The log file is named after the port's alias, or ttyUSBN when it
has none. A bench device's port is its `uart` in `tools/bench.py show <device>`.
A backpack's console is its name as the panel lists it (`bp:<name>` also
works); the uart server reaches it through the panel (`RELAY_PANEL_URL`).

## Log file layout

Each device's live log is at:

    /tmp/uart-logs/<device>.log

Rotated / saved snapshots live alongside, named:

    /tmp/uart-logs/<device>-<tag>.log

`<tag>` can be anything (`vendor-cold-boot`, `ddr-fail-cycle-3`,
`pre-fix`); if you don't pass one to `/uart fresh`, the skill auto-
generates a `YYYYMMDD-HHMMSS` timestamp.

## Logging basics

Steps for `start`:
1. `mcp__uart__stop_uart_log` device=<device>     # idempotent stop
2. `mcp__uart__start_uart_log` device=<device>
3. Confirm started.

Steps for `stop`:
1. `mcp__uart__stop_uart_log` device=<device>

Steps for `read`:
1. `mcp__uart__get_uart_log` device=<device>, lines=<lines or 40>
2. Display, stripping ANSI escapes.

Steps for `status`:
1. `mcp__uart__list_uart_devices`

## Log rotation - `fresh`

Use BEFORE each test cycle when you want a clean log but want to
keep the previous cycle's output for later comparison.

Steps for `fresh <device> [tag]`:
1. Derive tag: use `[tag]` if provided, else `$(date +%Y%m%d-%H%M%S)`.
2. Stop the live capture (lets us safely rename the file):
   ```
   mcp__uart__stop_uart_log device=<device>
   ```
3. Move the current log to the tagged copy:
   ```bash
   if [ -s /tmp/uart-logs/<device>.log ]; then
       mv /tmp/uart-logs/<device>.log /tmp/uart-logs/<device>-<tag>.log
   fi
   ```
   (If the log is empty there's nothing to save - just skip.)
4. Restart capture (creates a fresh empty log):
   ```
   mcp__uart__start_uart_log device=<device>
   ```
5. Report the saved path so the user (and future you) can find it.

## Snapshot - `save`

Use AFTER a test cycle produced something interesting and you want
to keep it before continuing. Doesn't interrupt the live capture.

Steps for `save <device> <tag>`:
1. Require `<tag>` (no auto-timestamp here - the save is an
   intentional named snapshot).
2. `cp /tmp/uart-logs/<device>.log /tmp/uart-logs/<device>-<tag>.log`
3. Report the saved path.

## When to use which

| Want                                  | Use                                |
|---------------------------------------|------------------------------------|
| Start a brand-new capture session     | `/uart start`                      |
| Clear the log between two test cycles, keep history | `/uart fresh <dev> [tag]` |
| Snapshot the current log without breaking the stream | `/uart save <dev> <tag>` |
| Read what the device has output       | `/uart read <dev>`                 |
| Clear the in-MCP read buffer only (NOT the log file) | `/uart clear <dev>`  |
| Discard the whole log AND the buffer  | `/uart stop` + delete file + `/uart start` (or `/uart fresh`) |

Notes:
- `clear` only resets the `mcp__uart` MCP's read offset for
  `read_uart` / `read_uart_lines` APIs. It does **not** touch the
  on-disk log file used by `get_uart_log` / shell tools.
- `fresh` is what you want at the start of each test cycle - it
  saves history AND gives you a clean log to read against.

## Interactive / U-Boot

Steps for `uboot <bench device>`:
1. `<repo>/tools/bench.py show <bench device>` (this skill lives at
   `<repo>/skills/uart/`; resolve symlinks with `readlink -f`). It needs a
   `uart`; without one, find the port first (`tools/uart-listen-all.py`).
2. `mcp__uart__clear_uart_buffer` device=<uart>
3. `mcp__relay-panel__set_relay` its `power` relay `off`; wait 2 seconds.
   (Backpack: skip this; step 4 uses `backpack_action action=power-cycle`.)
4. Power on AND interrupt boot together, in one message:
   - `mcp__relay-panel__set_relay` `power` relay `on` (backpack:
     `mcp__relay-panel__backpack_action action=power-cycle`)
   - `mcp__uart__uart_interact` device=<uart>, clear=true, timeout=30, script:
     ```json
     [{"send_until": "\\x03", "expect": "\\w+# $|=> $", "interval": 0.1}]
     ```
   Some U-Boot builds stop on a key other than Ctrl-C (a space, say); send
   that instead if Ctrl-C does nothing.
5. Confirm the U-Boot prompt was reached.

Steps for `send <device> <command>`:
1. `mcp__uart__uart_interact` device=<device>, timeout=5, script:
   ```json
   [{"send": "<command>", "expect": "\\w+# $|=> $|~ #|login:"}]
   ```
2. Display the transcript.

## Key patterns

- **U-Boot prompt**: `\w+# $|=> $` - matches `isvp_t31# `, `isvp_a1# `, `=> `, etc.
- **Linux root shell**: `~ # $` or `\w+:~# `
- **Login prompt**: `login:\s*$`
- **Do NOT use** `#` or `=>` alone - they false-match on boot
  banners like `###### Press Ctrl-C ######`.

## Tool reference

| Tool | Purpose |
|---|---|
| `mcp__uart__uart_interact` | Multi-step scripted interaction (send/expect/wait/send_until) |
| `mcp__uart__uart_expect` | Send + wait for one pattern |
| `mcp__uart__write_uart` | Fire-and-forget send |
| `mcp__uart__read_uart` | Read live output for N seconds |
| `mcp__uart__clear_uart_buffer` | Discard stale MCP read buffer (NOT the log file) |
| `mcp__uart__start_uart_log` | Start background capture to file |
| `mcp__uart__stop_uart_log` | Stop background capture |
| `mcp__uart__get_uart_log` | Read captured log file |
| `mcp__uart__list_uart_devices` | List ports, aliases and capture state |
| `mcp__uart__name_uart` | Set or remove an optional alias for a port |

## uart_interact script steps

Each step is a dict with optional keys:
- `wait`: regex - wait for pattern before proceeding
- `send`: string - data to send (escape sequences: `\\x03`=Ctrl-C,
  `\\r`, `\\n`, `\\x1b`=ESC)
- `expect`: regex - wait for pattern after sending
- `send_until`: string - send repeatedly until `expect` matches
  (for boot interrupt)
- `interval`: float - seconds between send_until repeats (default 0.2)
- `delay`: float - seconds to pause before this step

Auto `\r` is appended to `send` unless it ends with `\r`/`\n` or is
a single control char.
