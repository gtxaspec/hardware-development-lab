# Running a bench

How the pieces in this repo fit together for SoC bring-up and flash testing,
and the working habits that keep a bench fast. Your own bench's specifics
(which board is on which relay, quirks per unit) go in `bench.toml`, not here;
see `bench.example.toml`.

## The parts

| Part | Where | Does |
|---|---|---|
| Relay panel | Pi, `relay-panel.py` | power and boot-pin relays, mux GPIOs, LED strips, read-only USB view |
| ser2net | Pi, `pi/ser2net.yaml` | each `/dev/ttyUSBN` on raw TCP `3000+N` |
| `ch348-order` | Pi, `pi/` | stable `ttyUSB` numbering across boots |
| `dfu-remote` | Pi, from [thingino-dfu](https://github.com/gtxaspec/thingino-dfu) | owns the USB bus; bootstraps and flashes for the `thingino-dfu` client |
| relay-panel MCP | dev machine, `mcp-server.py` | the panel API as Claude Code tools |
| uart MCP | dev machine, `mcp/uart/` | console read/write, background logs |
| usb-hotplug MCP | dev machine, `mcp/usb-hotplug/` | USB add/remove events, `usb_wait` |
| Backpacks | ESP32 per camera, [thingino-backpack](https://github.com/thingino/thingino-backpack) | a camera's power, boot pin, USB (with its own `dfu-remote`) and console, over the network; the panel finds and drives them |
| skills | dev machine, `skills/` | `/flash`, `/bootstrap`, `/poweroff`, `/uart`, `/capture`, `/deploy-dfu` |
| `bench.toml` | repo root, gitignored | device name -> relays, mux, console port |

Each device needs two relays: one switching its power, one shorting a boot
pin (on Ingenic, typically SPI flash DI to ground) so the bootrom falls back
to USB boot. A camera powered over USB gets that power through a TS3USB30
breakout's common 5 V rail, which also lets a GPIO steer its USB data between
the Pi (for flashing) and a peripheral; the breakout switches data only, the
relay switches the power (README, "One camera on USB"). Bench-wide rails (a 5 V or 12 V supply on a relay) carry
"master" in their panel name and are never switched by the skills.

## Setting up bench.toml

1. Name the relays in the panel UI after the devices they drive.
2. Copy `bench.example.toml` to `bench.toml`; add one `[devices.<name>]` per
   unit with its `power` and `boot` relays, or, for a camera on a backpack,
   `backpack = "<name>"` (the name the panel lists it under).
3. `tools/bench.py check` reads the live panel and prints the panel's name
   for every relay you listed, flagging clashes and rails, and whether each
   named backpack is there and online.
4. Leave `uart` unset until you have seen the board's boot banner on a port
   (`tools/uart-listen-all.py` while you power it). A wrong console mapping
   is worse than none: silence on the wrong port reads as "board dead".

## Into the bootrom

1. Mux GPIO (if any) to the host side; detach relay (if any) open.
2. Power off and boot pin on, together.
3. Wait `settle_secs`, power on.
4. `usb_wait` for the bootrom's USB id. Never a fixed sleep: some bootroms
   take 80 s, most under 10.
5. Boot pin off. Required before any flash write.

On a backpack all five steps are one call, the unit's `bootrom` action (the
card's **Enter bootrom**): it holds the pin, power-cycles, and lets go the
moment the bootrom enumerates on its own USB port.

## Development loops, fastest first

**USB boot, no flash write** (~5 s a cycle): with the device in the bootrom,
`thingino-dfu --host <dfu_host> --spl <spl> --uboot <uboot>` loads both into
RAM and runs them. Rebuild and re-run; no relay changes while the bootrom is
still listening. Power-cycle into the bootrom only when the device has
dropped off USB.

**Flash and cold boot** (~15-25 s): `/flash <device> <image>`, which ends
with a power cycle with the boot pin released. Use it only when the flashed
path itself is under test (SPL header layout, boot from flash).

## Habits

- Assume the code is wrong, not the hardware. Prove the hardware with the
  vendor's own image before blaming a board, and record each unit as
  verified on stock firmware when it joins the bench.
- Keep a UART log streaming for the whole session and rotate it between
  cycles (`/uart fresh <port> <tag>`) so each cycle reads clean and the last
  one is kept.
- Send independent relay switches in one message; they run in parallel.
- Do not restart the panel service mid-test: startup forces every relay and
  LED channel off. `deploy.py` snapshots and restores them.
- Do not stop `dfu-remote` as cleanup; it is meant to stay up.

## Site notes

Keep a short per-site file next to your `bench.toml` (gitignored if it names
machines) for what does not fit a TOML field: which board each unit really
is, its flash and DDR, what it was last flashed with, known faults. One
heading per device, newest note first, dated.
