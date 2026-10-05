# Relay Control Panel

Web panel, HTTP API and MCP server for the camera test bench: I²C relay boards,
direct GPIO pins, dimmable LED strips, live UART consoles, and a view of which
devices are sitting in a flashable USB state.

It runs on a single-board computer wired into the bench — an Orange Pi here, but
anything with I²C and a `gpio` CLI will do — and serves on port 8080.

![The panel: two relay boards in PCB view, GPIO pins, LED strips with a group
master, and connected USB devices](docs/screenshot.png)

Try it with no hardware at all:

```bash
./demo.py            # stubs out I²C and GPIO, prints a URL to open
```

## What it drives

| | Hardware | Addressing | Control |
|---|---|---|---|
| **Relays** | XL9535 16-channel I²C boards | bus + address `0x20`–`0x27` | on / off / toggle, per board or all |
| **GPIOs** | Orange Pi header pins via `wiringOP` | wiringPi number | on / off / toggle |
| **LED PWM** | PCA9685 16-channel, driving 12 V strips through MOSFET modules | bus + address, typically `0x40` | 12-bit duty per channel, 24–1526 Hz, channel groups |
| **USB** | Ingenic bootrom / U-Boot DFU gadget | read-only | shows what is flashable right now |
| **UART** | Raw TCP consoles provided by ser2net | `ttyUSB0`–`ttyUSB23`, ports 3000–3023 by default | read-only live output, individually or all together |

The PWM section exists to simulate day/night transitions for camera testing:
continuous, repeatable light levels rather than the relay-binary room light the
bench had before.

## Sample topology

One arrangement that works. Everything hangs off a single I²C bus plus a few
GPIO pins, so the whole bench is three wires from the SBC.

```
  ┌─────────────────────────────────────────┐
  │  Single-board computer                  │
  │  Orange Pi 3B here; anything with I²C   │
  │  relay-panel.py, port 8080              │
  └──┬───────────────────────────┬──────────┘
     │ I²C bus (SDA / SCL / GND) │ GPIO header pins, 3.3 V
     │ 3.3 V logic               └──────► TS3USB30 1:2 USB mux
     │                                    (select pin routes a device's
     │                                     USB to one host or the other)
     ├──────────────┬──────────────┬─────────────────┐
  ┌──▼───────┐  ┌───▼──────┐  ┌────▼─────┐   ┌───────▼────────┐
  │ XL9535   │  │ XL9535   │  │ XL9535   │   │   PCA9685      │
  │  0x20    │  │  0x21    │  │  0x27    │   │    0x40        │
  │ 16 relay │  │ 16 relay │  │ 16 relay │   │ 16 × 12-bit PWM│
  └──┬───────┘  └───┬──────┘  └────┬─────┘   └───────┬────────┘
     │              │              │                 │ 3.3 V gate drive
     ▼              ▼              ▼                 ▼
  device 5 V    device 12 V   boot-mode      ┌──────────────────┐
  power rails   power rails   straps to GND  │ D4184 dual-MOSFET│ one module
                                             │ trigger modules  │ per channel
                                             └────────┬─────────┘
                                                      │ switches the LOW side
                                                      ▼
                                             ┌──────────────────┐
                                             │ 12 V LED strips  │
                                             │ white + RGB      │
                                             └──────────────────┘
                                                      ▲
                                             12 V PSU ┘ (+ to strip directly)
```

### Bill of materials

| Part | Role | Notes |
|---|---|---|
| Single-board computer | Runs the panel | Needs I²C and, for the GPIO section, a `gpio` CLI |
| **TS3USB30** 1-to-2 USB switch breakout | Routes a device's USB between two hosts | Driven from one GPIO. 3.3 V logic, so straight off the header |
| **XL9535** 16-channel I²C relay board | Switches device power and boot-mode straps | Addresses `0x20`–`0x27` via jumpers, so up to 8 boards per bus |
| **PCA9685** 16-channel PWM board | Dims the LED strips | Address `0x40`–`0x7F` via jumpers |
| **D4184** dual-MOSFET trigger module (5–36 V, 15 A) | Power stage between PWM and strip | One per PWM channel. Low-side switching, rated to 20 kHz |
| 12 V PSU | LED strips | Sized for total strip current |
| 5 V / 12 V PSUs | Devices under test | Switched by the relay boards |
| 12 V LED strips | The light source | White for luma work, RGB for colour |

### Wiring notes

**Common ground everywhere.** SBC, relay boards, PCA9685, MOSFET modules and
both PSUs. The MOSFET switches the low side, so its gate reference and the PWM
reference have to be the same node or nothing works predictably.

**No gate pulldowns needed with a PCA9685.** In its default totem-pole mode
(`MODE2` `OUTDRV=1`) it actively drives both rails rather than floating, the
`LEDn` registers come out of power-on reset set to `FULL_OFF`, and with
`OUTNE=00` the outputs sit LOW even while `/OE` is high. The gates are held down
from reset onwards, and this bench runs without external pulldowns.

Add a 10 kΩ pulldown per gate only if that stops being true: `OUTDRV=0`
(open-drain), `OUTNE=10` (outputs high-impedance when disabled), or driving the
modules from a microcontroller whose pins are inputs until firmware configures
them — that last one is a real window of hundreds of milliseconds during which
the strips will flash on, and firmware cannot fix a state that exists before
firmware runs.

**Leave the PCA9685's `V+` pin unconnected.** It only matters if you drive LEDs
from the chip directly. Driving MOSFET gates, the logic supply is all it needs.

**I²C pull-ups.** Most breakout boards include them. Several boards on one bus
means several pull-ups in parallel — if the bus misbehaves, that is the first
thing to check.

**The relay board's logic supply and its relay coil supply may differ.** Common
boards want 5 V for the coils while accepting 3.3 V logic. Check before wiring.

**A USB mux is a select, not a power switch.** The TS3USB30's select pin routes
the device's data lines to one of two downstream ports, so the panel's `on` /
`off` for that GPIO means "port B" / "port A", not "USB energized" / "USB dead".
Name the GPIO after what selecting it *does* — the config's `name` is free text
for exactly this. The breakout also has an active-low `OE`; tie it low unless
you want a third, genuinely-disconnected state, in which case give it its own
GPIO.

### Worth adding for day/night work

Not required, but this rig is noticeably better with them:

- **A diffuser.** Bounce off a white card rather than pointing the camera at the
  strip. Direct view of LED point sources saturates pixels and gives auto-exposure
  statistics that mean nothing.
- **A lux sensor** (TSL2591 or VEML7700) on the same I²C bus, in the scene.
  Without independent ground truth the rig cannot tell "the camera changed" from
  "the rig drifted".
- **An 850 nm IR strip** on a spare channel. Neither a white nor an RGB strip
  emits IR, so this rig can drive a camera *into* night mode by going dark, but
  cannot reproduce an IR-illuminated night scene.
- **A fuse on the 12 V rail**, sized under the strip's total draw.

## Layout

```
relay-panel.py       the server: HTTP API + hardware access. Pure stdlib.
uart_stream.py       on-demand, read-only TCP console fan-out
static/index.html    the whole web UI, single file, no build step
mcp-server.py        MCP server exposing the API as tools (30 of them)
deploy.py            state-preserving deploy to the Orange Pi
demo.py              runs the panel against stubbed hardware
test-pwm.py          pure-logic tests, no hardware, run anywhere
test-concurrency.py  bus priority and sampler tests, no hardware
test-regressions.py  file serving, relay races, sample ordering, startup and demo tests
test-ui-reliability.js browser regressions; starts its own simulated panel
test-uart.py         UART fan-out, reconnect, cleanup and access-control tests
test-uart-ui.js      browser console tests; starts simulated UARTs via demo.py
test-hardware.py     hardware tests, drives the real PCA9685
relay-panel.service  systemd unit
API.md               full HTTP API reference
docs/screenshot.png  the image above, regenerated by demo.py
```

`relay-panel.py` deliberately has **no third-party dependencies**. It shells out
to `i2ctransfer`/`i2cget`/`i2cset`/`i2cdump` and the `gpio` CLI, so the Orange Pi
runs it with the system Python and needs no venv, no pip, and no network to be
provisioned.
`requirements.txt` covers `mcp-server.py` only, which runs on the dev machine.

A background sampler reads the hardware once per second and shares its snapshot
between clients. Responses report the snapshot's age and which writes it covers;
`/api/state?fresh=1` requests an immediate hardware read.

## Deploying

```bash
cp .env.example .env     # set RELAY_PANEL_SSH=root@your-pi, gitignored
./deploy.py              # copy what changed, restart only if needed
./deploy.py --dry-run    # show what would be copied
```

`.env` is how a checkout remembers which bench it belongs to without the
hostname reaching version control. A real environment variable, or `--host`,
overrides it for a one-off.

Read this before deploying by hand:

> **Restarting the service forces every relay and every PWM channel OFF.**
> `init_boards()` and `init_pwm()` treat a restart as a known-safe state, not a
> resume. On a bench where relays feed camera power, a careless restart
> power-cuts devices, and mid-flash that means a corrupted write.

`deploy.py` handles this: it refuses to run while a flash looks active, snapshots
what is on, restarts, restores, and *verifies the restore against the snapshot*
rather than assuming it worked. It also skips the restart entirely when only
`static/index.html` changed, since that is read from disk per request.

Nothing persists a duty or a relay state across a restart on purpose, so a stale
config can never bring a strip back up at an unexpected level.

## First-time install

On the Pi, enable I²C and install `i2c-tools` (including `i2ctransfer`, `i2cget`,
`i2cset` and `i2cdump`) and `wiringOP` (for the `gpio` CLI — only needed if you
want the GPIO section).
Then find your bus and what is on it:

```bash
i2cdetect -l              # which bus number your header is
i2cdetect -y -r 2         # what answers on it
```

The bus number and every address are site-specific: they are whatever your
boards' jumpers are set to. Nothing in this repo assumes a particular layout.

```bash
PI=root@your-pi
ssh $PI 'mkdir -p /opt/relay-panel/static'
scp relay-panel.py uart_stream.py $PI:/opt/relay-panel/
scp static/index.html $PI:/opt/relay-panel/static/
scp relay-panel.service $PI:/etc/systemd/system/
ssh $PI 'systemctl daemon-reload && systemctl enable --now relay-panel'
```

Then open `http://your-pi:8080` and add your boards, GPIOs and PWM chips from
the web UI. Nothing else needs editing — the config is built through the UI, not
checked in.

### Porting to other hardware

The code shells out to `i2ctransfer`/`i2cget`/`i2cset`/`i2cdump` and the `gpio`
CLI, so most of what varies between benches is outside this repo. What to check:

**Relay polarity — check this first.** The code treats a HIGH output as
energized. Many cheap relay modules are the opposite: they energize on a LOW.
On such a board every control inverts, and worse, startup writes `0x00` to the
output registers meaning to turn everything off, which would instead switch
everything **on**. There is no invert option yet. Verify your board's polarity
with a single relay before wiring anything that matters to it.

**The `gpio` CLI.** wiringOP on Orange Pi is a fork of Gordon Henderson's
WiringPi, so the verbs this code uses — `gpio mode <n> out`, `gpio write <n>
0|1`, `gpio read <n>` — are identical on both. **The pin numbers are not.** A
wiringPi number maps to different physical pins on different boards: wPi 27 is
physical pin 40 on an Orange Pi 3B and physical pin 36 on a Raspberry Pi. Run
`gpio readall` on your own board and use its numbers; the `pin` field in the
config is a display-only label, nothing is derived from it.

Note also that upstream WiringPi was deprecated in 2019 and recent Raspberry Pi
OS releases ship `libgpiod` (`gpioset`/`gpioget`) or `pinctrl` instead, so
`gpio` may not exist at all. Either install a maintained WiringPi fork or adapt
`gpio_cmd()` — it is the single function every GPIO call goes through.

**Relay expander family.** The relay code assumes a PCA9555/XL9535/TCA9535-style
16-bit expander: configuration at `0x06`/`0x07`, output at `0x02`/`0x03`. A
PCF8574 has no register file at all and will not work without changes.

**I²C bus number and addresses** are whatever your wiring and jumpers are; find
them with `i2cdetect -l` and `i2cdetect -y -r <bus>`.

**Python** 3.6+ for the server, which is pure standard library. `mcp-server.py`
needs 3.10+ for its type annotations.

**Permissions.** I²C and GPIO access needs root or membership of the `i2c` and
`gpio` groups. The systemd unit sets no `User=`, so it runs as root.

**`deploy.py`'s flash check** looks for connections to port 5050 and a USB
device with product ID `4d44`, which is this bench's flashing setup. Point
`RELAY_PANEL_FLASH_PORT` / `RELAY_PANEL_DFU_PID` at your own, or set the port to
`0` and the pid to empty to skip the check.

### Environment variables

All optional; the defaults suit a normal install.

| Variable | Default | Used by |
|---|---|---|
| `RELAY_PANEL_DIR` | `/opt/relay-panel` | server: install dir holding `config.json` and `static/` |
| `RELAY_PANEL_HOST` | `0.0.0.0` | server: listen address |
| `RELAY_PANEL_PORT` | `8080` | server: listen port |
| `RELAY_PANEL_USB_VID` | `a108` (Ingenic) | server: which USB vendor the USB section watches |
| `RELAY_PANEL_UART_HOST` | `127.0.0.1` | server: raw TCP UART provider, usually ser2net on the same Pi |
| `RELAY_PANEL_UART_BASE_PORT` | `3000` | server: TCP port for `ttyUSB0`; each subsequent UART adds one |
| `RELAY_PANEL_UART_COUNT` | `24` | server: UART count, 0–64; `0` disables UART access |
| `RELAY_PANEL_URL` | `http://localhost:8080` | MCP server: where the panel is |
| `RELAY_PANEL_SSH` | *(none)* | `deploy.py`: ssh target, usually set in `.env` |
| `RELAY_PANEL_FLASH_PORT` | `5050` | `deploy.py`: flashing daemon's port; `0` disables |
| `RELAY_PANEL_DFU_PID` | `4d44` | `deploy.py`: USB pid meaning "in DFU"; empty disables |

`config.json`, optional `tokens.json`, and `.env` hold site-specific settings and are
gitignored — nothing in version control names a particular machine.

## UART consoles

Open **UART consoles** below the hardware controls and select a `ttyUSB` button
to stream its output. **All UARTs** combines the consoles with a `[ttyUSB#]`
prefix on each line. The default mapping matches `uart-monitor`: ports
3000–3023 for `ttyUSB0`–`ttyUSB23`. The browser connects through the panel;
ser2net continues to manage baud rates and serial devices.

**Pop out** opens the currently selected console, including **All UARTs**, in
its own resizable window. It carries over the buffered output and keeps
streaming independently when the main panel switches ports or closes. Follow,
Clear, and Connect/Disconnect remain available. You can bookmark the window's
`?console=0` (or `?console=all`) URL; a reload starts fresh live output.

The viewer is read-only and sends no serial input. Connections open on demand;
multiple browser viewers share one upstream connection per UART. Closing the
section or clicking **Disconnect** stops that viewer. The last upstream viewer
disconnects shortly afterward, including for quiet consoles.

**Follow output** scrolls as data arrives; scrolling back turns it off.
**Clear** clears the visible buffer. Disconnections retry automatically, and
the status reports whether the TCP console is connected. A connected but quiet
console waits for new output; it does not prove the attached device is powered.

Output starts when you connect, with no history replay. The browser keeps up to
2,000 lines or 200,000 characters and trims very long lines. It renders plain
text with basic carriage-return/backspace handling and strips terminal escape
sequences; this is a log viewer, not a full-screen terminal emulator.

UART access follows the panel's existing trusted-network/token rules. Scoped
tokens need an explicit `"uarts": [0, 1, ...]` list in their `allowed` map;
existing relay-only tokens do not gain console access. See [API.md](API.md#uart-consoles)
for streaming events and token examples.

## Configuration

`/opt/relay-panel/config.json` on the Pi, written by the API, **not** in version
control — it describes one physical bench.

```jsonc
{
  "boards": [
    {"address": "0x20", "bus": 2, "size": 16, "name": "Bench Power",
     "layout": "board", "relays": {"0": "Camera 1", "7": "5V Rail"}}
  ],
  "gpios": [
    {"wpi": 27, "pin": 40, "name": "USB Switch", "state": false}
  ],
  "pwm": [
    {"address": "0x40", "bus": 2, "name": "LED Strips", "freq_hz": 1526,
     "channels": {
       "0": {"name": "White Strip"},
       "1": {"name": "RGB Red"}, "2": {"name": "RGB Green"}, "3": {"name": "RGB Blue"}
     },
     "groups": [{"name": "RGB Strip", "channels": [1, 2, 3]}]}
  ]
}
```

Which PWM channels a chip manages comes from the `channels` map, not a count, so
they need not be contiguous — 0–3 for two strips and 7 for an IR bank with
nothing between is normal. Older configs using a `size` field still work and are
expanded on first edit.

## Using the panel

Click a relay or GPIO card to toggle it. Drag a channel slider to dim a strip; a
**group** card drives several channels from one master.

Relay and GPIO clicks send an explicit ON/OFF target and show **Turning on…** or
**Turning off…** until a hardware reading confirms the result. The compact PCB
view uses **On…** / **Off…** beside the indicator, with the full message on hover.
That control
ignores additional clicks while pending. Failed or timed-out readings show
**Unverified**, retaining the last known state when available; they never turn
an unavailable reading into a confirmed OFF. Polling continues during slider
use and inline editing without replacing the active control.

**Edit mode** (the `Edit` button, or `E`) reveals everything structural: rename
by clicking a card, edit a board's address by clicking its address line, and the
`×` / `Remove` / `+ Ch` / `+ Group` controls. Keeping those behind Edit is why
you cannot delete a channel by fumbling a slider.

Keyboard: arrows navigate, `Enter` toggles, `A` all on, `Z` all off, `E` edit mode.

## LED PWM notes

Frequency comes from dividing a 25 MHz oscillator by an integer, giving
**24 Hz to 1526 Hz** at a full 12 bits of duty at every frequency. The steps are
uneven and coarse at the top, so the API always reports the frequency actually
achieved rather than the one requested — a rig that logs what it set should log
the truth.

1526 Hz is comfortably fast enough for camera work: a 655 µs period against a
33 ms exposure bounds integration ripple near 2%. The low end is useful on
purpose — 24 Hz makes rolling-shutter banding appear on demand, so the rig can
reproduce the artifact rather than only avoid it.

Channels default to a **quarter-period stagger** (0, 1024, 2048, 3072) so ones
lit together spread their on-times across the cycle instead of stacking at count
0, which lowers both peak current draw and combined optical ripple. Three
channels driven as one white light spread best at exact thirds (0, 1365, 2731).

Group members carry a **weight**, defaulting to full. Equal weights mean the
master sets every member the same; unequal weights make it scale them
proportionally, holding a calibrated white point while dimming. The master is
derived from the hardware, never stored, so a channel nudged on its own shows up
as `mixed` instead of leaving a stale master behind.

The PCA9685 needs no external gate pulldowns in its default totem-pole mode —
see [Wiring notes](#wiring-notes) for when that stops being true.

## Safety rails

- Adding or moving a PWM chip onto an address already claimed on that bus is
  refused, **relay boards included** — a PCA9685 write aimed at an XL9535 lands
  on its output registers and power-cycles whatever is plugged into it.
- Removing a channel blanks it and drops it from any group, removing a group
  left with no members, so nothing points at a channel that is gone and no
  channel is left lit that nothing manages.
- A failed I²C read raises rather than reading as zero. A wedged bus used to
  display as every relay being off, which is the worst thing to report
  confidently on this bench.

### If the whole I²C bus goes quiet

Disturbing the bench cabling can wedge the controller — every device on the bus
stops answering at once and `dmesg` shows controller timeouts (on Rockchip,
`rk3x-i2c ...: timeout, ipd: 0x00, state: 4`). Reset it without rebooting by
unbinding and rebinding the controller. Find the driver and device names first,
since they vary by SoC:

```bash
ls /sys/bus/platform/drivers/ | grep i2c        # e.g. rk3x-i2c
ls /sys/bus/platform/drivers/rk3x-i2c/          # e.g. fe5b0000.i2c

echo fe5b0000.i2c > /sys/bus/platform/drivers/rk3x-i2c/unbind
sleep 1
echo fe5b0000.i2c > /sys/bus/platform/drivers/rk3x-i2c/bind
```

Relay outputs survive, because the XL9535s hold their register state while
powered. If a rebind does not fix it, suspect a slave holding SDA low, which
needs a power cycle instead. Prefer this over a reboot, which would also restart
the service and force everything off.

## Demo mode

`relay-panel.py` reaches hardware only by shelling out to `i2ctransfer`, `i2cget`,
`i2cset`, `i2cdump` and `gpio`. `demo.py` puts fakes for those tools on `PATH`
ahead of the real ones and points the server at a throwaway config, so the whole
UI runs on a machine with no I²C bus — and a bench with real hardware is never
touched.

It also starts 24 simulated TCP UARTs on loopback and points the demo server at
them, so console tests never connect to the bench's serial devices.

```bash
./demo.py                                    # serve, prints the URL
./demo.py --screenshot docs/screenshot.png   # regenerate the image above
```

Writes go through the real code path: the stub decodes the PCA9685 registers
the server writes and reflects them back, so the sliders and group master
behave as they do on hardware rather than being faked.

## Testing

```bash
python3 test-pwm.py                              # 79 pure-logic checks, no hardware
python3 test-concurrency.py                      # bus priority and sampler checks
python3 test-regressions.py                      # file serving, relay races, sample ordering, startup and demo
node test-ui-reliability.js                      # Playwright, automatically starts demo.py
python3 test-uart.py                             # local TCP fixtures, no hardware
node test-uart-ui.js                             # Playwright, simulated consoles only
ssh root@your-pi python3 - < test-hardware.py    # 63 checks, drives the real chip
```

The browser suite uses simulated hardware only. It checks pending controls,
delayed replies, stale and failed readings, restart recovery, polling overlap,
slider recovery, and editing/focus preservation. Install Playwright separately
or set `RELAY_PANEL_PLAYWRIGHT` to its module path; the suite reports a skip when
it is unavailable.

The UART suites check shared upstream connections, read-only behavior, UTF-8
fragments, authorization/revocation, reconnects, port switching, bounded buffers,
scrolling, narrow screens, and isolation from relay keyboard shortcuts.
They also cover separate console windows, scrollback transfer, popup blocking,
window resizing, and token entry without starting hardware polling.

`test-hardware.py` snapshots the whole PWM configuration and restores it in a
`finally` block, so a failure mid-run cannot leave the bench half-configured. It
never touches relays or GPIOs, and uses `--spare` channels for add/remove work so
the live rig is left alone. Run it on the Pi for register-level checks; run it
anywhere else and those skip.

## MCP server

Exposes the API as tools for Claude Code.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Register it in `~/.mcp.json`, pointing `RELAY_PANEL_URL` at the panel — the
default is `localhost`, which is only right if the MCP server runs on the Pi
itself:

```json
{"mcpServers": {"relay-panel": {
  "command": "/path/to/relay-panel/.venv/bin/python",
  "args": ["/path/to/relay-panel/mcp-server.py"],
  "env": {"RELAY_PANEL_URL": "http://your-pi:8080"}}}}
```

It is a thin HTTP client — it runs on the dev machine and nothing about it is
deployed to the Pi, so editing it takes effect on the next MCP reload rather
than a deploy.

Adding, removing and re-addressing boards and PWM chips is deliberately **not**
exposed as tools. That is physical setup, done in the web UI.

## API

See [API.md](API.md).

## Prior art

The relay layer began as [xl9535-relay-controller][x], a bash script driving the
same four registers — direction at `0x06`/`0x07`, output at `0x02`/`0x03` — and
was reimplemented here in Python. That script is not a dependency; nothing in
this project shells out to it.

[x]: https://github.com/gtxaspec/xl9535-relay-controller

## License

MIT. See [LICENSE](LICENSE).
