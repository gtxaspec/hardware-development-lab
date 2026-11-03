---
name: bootstrap
description: Power-cycle a bench device and bootstrap it into U-Boot DFU without writing flash
user-invocable: true
---

Bring a device up into U-Boot's DFU gadget without writing anything: for
inspection, a flash dump (`-r`), or USB-boot development.

Usage: `/bootstrap <device>`

## Steps

1. Do "Bench" and "Enter the bootrom" (or "Enter the bootrom: backpack", for a
   device with `backpack` set) from `../flash/SKILL.md` (relative to this
   skill's resolved directory), using tag `pre-bootstrap` for the UART log.
2. Bootstrap:
   ```bash
   thingino-dfu --host <dfu_host> -b
   ```
   - Dump the flash once up: `thingino-dfu --host <dfu_host> -r /tmp/<device>-dump.bin`
   - Legacy cloner instead: add `--cloner`.
3. If the device has `uart`: `mcp__uart__get_uart_log device=<uart> lines=30`.
4. Report the thingino-dfu output and the UART tail.

The device stays in DFU until power-cycled; `/poweroff <device>` when done.
