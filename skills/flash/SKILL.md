---
name: flash
description: Flash firmware to a bench device via DFU (default) or the legacy cloner
user-invocable: true
---

Power-cycle a bench device into its bootrom and write firmware with the
`thingino-dfu` client, which streams to the `dfu-remote` daemon on the Pi.
DFU is the default; `--cloner` is the legacy fallback, used only when asked or
when DFU cannot come up.

Usage:
- `/flash <device> <firmware_file>` - DFU
- `/flash cloner <device> <firmware_file>` - legacy cloner

`firmware_file` is a path on THIS host; the client streams it, nothing is
copied to the Pi.

## Bench

This skill lives at `<repo>/skills/flash/` (resolve symlinks with
`readlink -f`). Look the device up with:

```bash
<repo>/tools/bench.py show <device>
```

It prints `power` and `boot` (`board`/`relay`), optional `usb_mux`, `detach`
and `uart`, plus `settle_secs`, `enumerate_timeout_secs`, `bootrom_vid_pid`,
`dfu_host` and `notes`. Read the notes. Unknown device: `bench.py list`.

Never switch a relay whose panel name contains `master_name_contains`: those
are the bench rails. Only the device's own relays move.

## Enter the bootrom

1. If the device has `uart`: `/uart fresh <uart> pre-flash`, so this cycle
   gets a clean log.
2. If it has `usb_mux`: `mcp__relay-panel__set_gpio wpi=<wpi> state=<host_state>`.
3. If it has `detach`: `mcp__relay-panel__set_relay` that relay `off`.
4. Power off and boot pin on, in parallel:
   `set_relay <power> off`, `set_relay <boot> on`.
5. Wait `settle_secs`, then `set_relay <power> on`.
6. Wait for the bootrom, never a fixed sleep:
   `mcp__usb-hotplug__usb_wait action=add vid_pid=<bootrom_vid_pid> timeout_s=<enumerate_timeout_secs>`
7. Release the boot pin: `set_relay <boot> off`. Required before any write:
   while held it can short a flash pin and every write fails.

## Write

```bash
thingino-dfu --host <dfu_host> -w <firmware_file>
```

`-w` detects the SoC, bootstraps bootrom to U-Boot DFU, then writes. Give the
command a long timeout (~600 s). Several devices in bootrom at once:
`thingino-dfu --host <dfu_host> -l`, then add `-i <index>`.

Cloner instead: `thingino-dfu --host <dfu_host> --cloner -w <firmware_file> --cpu <soc>`.

## Cold boot

After a DFU write the device sits in DFU mode. Power-cycle it (boot pin is
already off): `set_relay <power> off`, then `on`. If it has `detach`, close
that relay again once the device is up. Report the thingino-dfu result and,
if it has `uart`, the boot banner from `mcp__uart__get_uart_log device=<uart> lines=40`.

## When it fails

- Connection refused: `dfu-remote` is not running on `dfu_host`. Restart it
  there (`systemctl restart dfu-remote` if installed as a unit, else
  `/deploy-dfu`). Do not stop it as a cleanup step; it is meant to stay up.
- No bootrom within the timeout: check the `usb_mux` GPIO state and that the
  boot pin relay is wired; cycle once more.
- DFU gadget never appears after bootstrap: usually DDR init failing in SPL.
  Show the UART log and offer the cloner.
- Nothing on the UART after cold boot: the new image did not run.
