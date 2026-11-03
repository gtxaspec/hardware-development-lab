# Hardware Development Lab

The software for a camera bring-up bench. At its centre is a relay panel: a web
UI and HTTP API for I²C relay boards, direct GPIO pins, dimmable LED strips, live
UART consoles, and a view of which devices are sitting in a flashable USB state.
It also drives [thingino backpacks](#backpacks): ESP32 units, each strapped to
one camera, that switch its power and boot pin and serve its USB and console
over the network, so a camera can sit anywhere on the network rather than on the
bench. Around it are Claude Code MCP servers and skills that drive whole flash
and boot cycles, and the Pi-side serial setup.

The panel runs on a single-board computer wired into the bench — an Orange Pi
here, but anything with I²C and a `gpio` CLI will do — and serves on port 8080.

![The panel: two relay boards in PCB view, GPIO pins, LED strips with a group
master, a backpack's card, Ingenic devices in USB boot, and every console streaming](docs/screenshot.png)

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
| **UART** | Raw TCP consoles provided by ser2net | `ttyUSB0`–`ttyUSB23`, ports 3000–3023 by default | live output, individually or all together; typing when switched to interactive |
| **Backpacks** | [thingino backpack](https://github.com/thingino/thingino-backpack) ESP32 units, one per camera | found over mDNS, or added by address | camera power, boot pin, enter bootrom, power cycle, USB view, console; flashing goes to the unit |

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

## Setting up a bench from scratch

Two machines are involved. Everything on the Pi is controlled from the dev
machine, where Claude Code runs.

| Runs on | What | Needs |
|---|---|---|
| Pi (any SBC with I²C) | `relay-panel.py`, ser2net, `ch348-order`, `dfu-remote` | Python 3.7+, `curl` (for `deploy.py`), `i2c-tools`, I²C enabled in the device tree, `wiringOP` (GPIOs only), `ser2net` 4.x, `usbutils`, `udev`; `tcpdump` + the `usbmon` module for `/capture` |
| Dev machine | the three MCP servers, the skills, `tools/`, the `thingino-dfu` client | Python 3.11+, [Bun](https://bun.sh), `socat` + `dialog` (`uart-monitor`), key-based root ssh to the Pi, [thingino-dfu](https://github.com/gtxaspec/thingino-dfu) installed (its build toolchain too, for `/deploy-dfu`) |

In order:

1. **Panel on the Pi**: [First-time install](#first-time-install). Open the UI,
   add the relay boards, GPIOs and PWM chips, and name every relay after the
   device it drives (rails get "master" in the name).
2. **Serial consoles on the Pi**: [Serial consoles](#serial-consoles-ser2net).
3. **Flashing daemon on the Pi**: install `dfu-remote` from thingino-dfu (a
   release, or `/deploy-dfu` once the rest is up) and run it as a service on
   port 5050: `scp pi/dfu-remote.service $PI:/etc/systemd/system/`, then
   `systemctl enable --now dfu-remote`.
4. **Dev machine**: the MCP venv and Bun packages, then register all three
   servers in `~/.mcp.json` ([MCP server](#mcp-server),
   [UART and USB MCP servers](#uart-and-usb-mcp-servers)).
5. **Bench map**: `bench.toml` and `tools/bench.py check`
   ([Skills and the bench map](#skills-and-the-bench-map)).
6. **Consoles**: power each board with `tools/uart-listen-all.py` running, then
   put the port it spoke on into that device's `uart` in `bench.toml`.
7. **Skills**: link them in and try `/poweroff all`, then `/bootstrap <device>`.
8. **Backpacks** (optional): flash thingino-backpack firmware onto an ESP32 per
   camera and join it to the network; the panel adds it by itself within a
   minute ([Backpacks](#backpacks)). In `bench.toml`, give that camera
   `backpack = "<name>"` instead of relays.

Before exposing the panel beyond your own network, read [Access](#access).

## Layout

```
relay-panel.py       the server: HTTP API + hardware access. Pure stdlib.
uart_stream.py       on-demand TCP console fan-out and input
backpacks.py         thingino backpacks: polling, actions, mDNS discovery
static/index.html    the whole web UI, single file, no build step
mcp-server.py        MCP server exposing the API as tools (33 of them)
mcp/uart/            MCP server for the UART consoles, straight over ser2net
mcp/usb-hotplug/     MCP server pushing USB plug/unplug events (Bun)
pi/                  ser2net config, CH348 port ordering, dfu-remote unit for the Pi
tools/bench.py       reads bench.toml: list devices, show one, check against the panel
tools/uart-listen-all.py  find which board is on which port
tools/uart-monitor   terminal console viewer: pick a port, or watch them all labelled
skills/              Claude Code skills: /flash, /bootstrap, /poweroff, /uart, ...
bench.example.toml   the bench map format; copy to bench.toml (gitignored)
docs/bench.md        how the parts fit together, and bench habits
deploy.py            state-preserving deploy to the Orange Pi
demo.py              runs the panel against stubbed hardware
test-pwm.py          pure-logic tests, no hardware, run anywhere
test-concurrency.py  bus priority and sampler tests, no hardware
test-regressions.py  file serving, relay races, sample ordering, startup and demo tests
test-ui-reliability.js browser regressions; starts its own simulated panel
test-uart.py         UART fan-out, reconnect, cleanup and access-control tests
test-backpacks.py    backpack polling, actions, access, discovery, console takeover
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
`requirements.txt` covers the Python MCP servers only, which run on the dev machine.

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
scp relay-panel.py uart_stream.py backpacks.py $PI:/opt/relay-panel/
scp static/index.html $PI:/opt/relay-panel/static/
scp relay-panel.service $PI:/etc/systemd/system/
ssh $PI 'systemctl daemon-reload && systemctl enable --now relay-panel'
```

Then open `http://your-pi:8080` and add your boards, GPIOs and PWM chips from
the web UI. Nothing else needs editing — the config is built through the UI, not
checked in.

### Serial consoles (ser2net)

The UART consoles, the uart MCP server and `tools/uart-listen-all.py` all read
ser2net's raw TCP ports. `pi/` holds the Pi side:

```bash
scp pi/ser2net.yaml $PI:/etc/ser2net.yaml          # ttyUSB0-23 -> TCP 3000-3023
scp pi/ch348-order.sh $PI:/usr/local/bin/
scp pi/ch348-order.default $PI:/etc/default/ch348-order
scp pi/ch348-order.service $PI:/etc/systemd/system/
ssh $PI 'systemctl daemon-reload && systemctl enable ch348-order ser2net'
```

**CH348 adapters need a driver.** Kernels without `ch348` in
`drivers/usb/serial` see each adapter as nothing usable. Build the out-of-tree
driver (GPL-2.0, the version proposed for mainline) against your kernel's
headers; this bench runs commit `7ad39940`:

```bash
ssh $PI 'apt-get install -y build-essential git && \
  git clone https://github.com/xdarklight/ch348 /root/ch348 && cd /root/ch348 && \
  git checkout 7ad39940446f28eab060de33dbd714979ee869d1 && \
  make && make install && echo ch348 > /etc/modules-load.d/ch348.conf && modprobe ch348'
```

It needs the running kernel's headers (`/lib/modules/$(uname -r)/build`), and
has to be rebuilt after a kernel upgrade. Other adapters (CP210x, FTDI, CH341)
use drivers already in the kernel.

`ch348-order.sh` is for WCH CH348 8-port USB serial adapters. Linux numbers
`ttyUSB` in enumeration order, which can change between boots, so the script
re-authorizes the adapters one at a time in a fixed order: each lands on the
same block of eight every boot. Run `ch348-order.sh --dry-run` to see each
adapter's key, then list them in `/etc/default/ch348-order` in the order you
want. With nothing listed, adapters go in sorted key order. Adapters other than
the CH348 need their own rule (udev symlinks by serial number, typically).

To watch consoles from a terminal, `UART_HOST=your-pi tools/uart-monitor` gives
a menu of ports (`-a` for all of them labelled, `-i` to type into one).

After any re-wire, power the boards one at a time while
`UART_HOST=your-pi tools/uart-listen-all.py 40` runs; it prints which ports
spoke and the U-Boot CPU/model line each one printed, which is the quickest way
to rebuild a port-to-board map.

Flashing is a separate daemon, `dfu-remote` from
[thingino-dfu](https://github.com/gtxaspec/thingino-dfu); the panel's USB section
and `deploy.py` only watch it.

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

**Python** 3.7+ for the server, which is pure standard library. The MCP
servers need 3.10+, `tools/bench.py` 3.11+ (`tomllib`).

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
| `RELAY_PANEL_TRUSTED` | loopback + private ranges | server: networks with full access and no token; see [Access](#access) |
| `RELAY_PANEL_URL` | `http://localhost:8080` | MCP servers: where the panel is (the uart server needs it for backpack consoles) |
| `RELAY_PANEL_BACKPACK_DISCOVERY` | `1` | server: `0` stops looking for backpacks over mDNS |
| `RELAY_PANEL_USB_SYS` | `/sys/bus/usb/devices` | server: where USB devices are read from (`demo.py` points it at a fake tree) |
| `UART_HOST` | `localhost` | uart MCP: the ser2net host |
| `UART_BASE_PORT` | `3000` | uart MCP: TCP port for `ttyUSB0` |
| `UART_COUNT` | `24` | uart MCP: number of UART ports |
| `UART_ALIASES` | `mcp/uart/aliases.json` | uart MCP: optional port names |
| `USB_HOTPLUG_HOST` | `root@localhost` | usb-hotplug MCP: ssh target that runs `udevadm`/`lsusb` |
| `BENCH_TOML` | `bench.toml` at the repo root | `tools/bench.py` and the skills: the bench map |
| `RELAY_PANEL_SSH` | *(none)* | `deploy.py`: ssh target, usually set in `.env` |
| `RELAY_PANEL_FLASH_PORT` | `5050` | `deploy.py`: flashing daemon's port; `0` disables |
| `RELAY_PANEL_DFU_PID` | `4d44` | `deploy.py`: USB pid meaning "in DFU"; empty disables |

`config.json`, optional `tokens.json`, and `.env` hold site-specific settings and are
gitignored — nothing in version control names a particular machine.

## Backpacks

A [thingino backpack](https://github.com/thingino/thingino-backpack) is an
ESP32 strapped to one camera: it switches the camera's power, holds its boot
pin, hosts its USB port (and runs the flashing daemon on it), and serves its
console. The panel shows each one as a card: online state and firmware, power
and boot-pin switches, **Enter bootrom** (hold the pin, power-cycle, release
once the bootrom enumerates), **Power cycle**, the USB devices on its port, and
**Console**, which opens the unit's console in the console section.

- **Found automatically.** The panel asks the network for `_thingino._tcp`
  every minute and adds every `thingino-backpack-*` host that answers, and
  follows a unit to a new address. A unit you remove stays removed. In edit
  mode, **+ Backpack** adds one by address (for a unit on another network) and
  **Discover** looks now. Discovery is mDNS, so it only sees the Pi's own
  network segments.
- **Everything goes through the panel.** Browsers, the MCP servers and the
  skills never talk to a unit directly, so tokens apply (grant `"backpacks":
  [id, ...]`) and the unit's one-client console is held once and shared. A
  unit's console is numbered 100 + its id in the console list; if another
  client takes it over, the panel lets go and waits to be asked again rather
  than taking it back.
- **Flashing** goes straight to the unit: its card shows the `dfu` address for
  `thingino-dfu --host`.
- In `bench.toml`, a device with `backpack = "<name>"` replaces its power and
  boot relays; the skills then use the unit's `bootrom` action and DFU address.

## Access

The panel has no login. Any client whose address falls in
`RELAY_PANEL_TRUSTED` gets full control: every relay, GPIO, LED and console,
plus the structural actions. The default is loopback plus all private
ranges (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), which assumes the
panel is only reachable from a network you own.

Everyone else needs a bearer token from `tokens.json` in the install dir,
which maps the token's SHA-256 to what it may touch (`relays`, `pwm`,
`uarts`, `uart_input`, ...). See [API.md](API.md#uart-consoles) for the format.
To give an outside collaborator access over a VPN, narrow
`RELAY_PANEL_TRUSTED` to your own ranges first (a systemd drop-in with
`Environment=RELAY_PANEL_TRUSTED=...`), otherwise their VPN addresses may
already count as trusted.

Backpacks have no authentication of their own: the panel's `backpacks`
token grant covers them only for clients that go through the panel. Anyone who
reaches a unit directly can drive it.

Two things bypass the panel entirely: ser2net's TCP ports have no
authentication, and the usb-hotplug server uses root ssh. Firewall the Pi's
3000-30xx ports to the machines that should reach the consoles.

## UART consoles

Open **UART consoles** below the hardware controls and select a `ttyUSB` button
to stream its output. **All UARTs** combines the consoles with a `[ttyUSB#]`
prefix on each line. The default mapping matches `pi/ser2net.yaml`: ports
3000–3023 for `ttyUSB0`–`ttyUSB23`. The browser connects through the panel;
ser2net continues to manage baud rates and serial devices.

**Pop out** opens the currently selected console, including **All UARTs**, in
its own resizable window. It carries over the buffered output and keeps
streaming independently when the main panel switches ports or closes. Follow,
Clear, and Connect/Disconnect remain available. You can bookmark the window's
`?console=0` (or `?console=all`) URL; a reload starts fresh live output.

Connections open on demand; multiple browser viewers share one upstream
connection per UART. Closing the section or clicking **Disconnect** stops that
viewer. The last upstream viewer disconnects shortly afterward, including for
quiet consoles.

Consoles open read only. To type into one, tick **Interactive** in its toolbar
(single UARTs only, never **All UARTs**), then click the output. Keys go to the
device as a terminal sends them: Enter as CR, Backspace as DEL, arrows and
Home/End/Delete as escape sequences, Ctrl+letter as control characters. Ctrl+C
copies when console text is selected and interrupts otherwise; Ctrl+V pastes,
and a paste of more than one line asks first. Tab is sent to the device, so
Shift+Tab is the way out of the console. Switching UART, disconnecting, closing
the section, or popping out always starts read only again. The browser keeps
some shortcuts for itself, such as Ctrl+W, Ctrl+T and Ctrl+N, so those never
reach the device.

Input shares the upstream connection, and ser2net passes bytes from any of its
clients to the tty, so typing here mixes with anything else connected to that
port, the uart MCP server included. U-Boot does not read the UART while a command
runs, so a multi-line paste into it can lose characters after the first line.

**Follow output** scrolls as data arrives; scrolling back turns it off.
**Clear** clears the visible buffer. Disconnections retry automatically, and
the status reports whether the TCP console is connected. A connected but quiet
console waits for new output; it does not prove the attached device is powered.

Output starts when you connect, with no history replay. The browser keeps up to
2,000 lines or 200,000 characters and trims very long lines. It renders plain
text, applies the in-line edits a shell's line editor sends (backspace, cursor
left and right, erase to end of line, insert and delete characters), and strips
other terminal escape sequences. It is a log viewer, not a full-screen terminal
emulator: `vi`, `top` and menu-driven tools will not draw correctly.

UART access follows the [Access](#access) rules. Scoped
tokens need an explicit `"uarts": [0, 1, ...]` list in their `allowed` map;
existing relay-only tokens do not gain console access. Typing is a separate
grant, `"uart_input": [0, 1, ...]`, honored only for UARTs also in `uarts`; a
token without it stays read only. See [API.md](API.md#uart-consoles) for
streaming events, input, and token examples.

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

Every section collapses with the ▼ beside its name (collapsed, it shows how many
of its switches are on), and **Hide unnamed** hides relays still called
`Relay N`; edit mode shows them again for renaming. Both are remembered per
browser.

**On a phone** (under 600 px) the console docks to the bottom of the screen
while the controls scroll above it, so you can switch a relay and watch the boot
at once. Drag its handle to resize, ▾ shrinks it to its title bar without
disconnecting, ⤢ makes it full screen, and the port comes from a dropdown.
Relay boards show one line per relay, and `+ Board` / `+ GPIO` / `+ PWM` appear
only in edit mode.

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
them, so console tests never connect to the bench's serial devices, a simulated
backpack (its HTTP API and console), and a fake USB tree with Ingenic bootrom
and DFU-gadget devices (`RELAY_PANEL_USB_SYS` points the server at it).

Opening the page as `/#console=<n>` (or `#console=all`) opens that console as it
loads, on the demo and on a real panel alike; the screenshot uses it.

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
python3 test-backpacks.py                        # a simulated backpack, no hardware
node test-uart-ui.js                             # Playwright, simulated consoles only
ssh root@your-pi python3 - < test-hardware.py    # 63 checks, drives the real chip
```

The browser suite uses simulated hardware only. It checks pending controls,
delayed replies, stale and failed readings, restart recovery, polling overlap,
slider recovery, and editing/focus preservation. Both browser suites need
Playwright (`npm i --no-save playwright && npx playwright install chromium`),
or `RELAY_PANEL_PLAYWRIGHT` set to its module path; they report a skip without it.

The UART suites check shared upstream connections, the read-only default, UTF-8
fragments, authorization/revocation, reconnects, port switching, bounded buffers,
scrolling, narrow screens, and isolation from relay keyboard shortcuts. For
input they cover key and paste handling, ordered delivery over the viewer's
connection, line-editor redraws, dropped (never replayed) input after a
failure, and refusal of cross-site, malformed, and ungranted requests.
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

### UART and USB MCP servers

Two more servers live under `mcp/`. Both run on the dev machine and go in the
same `~/.mcp.json` as the panel's server; use absolute paths throughout.

**`mcp/uart/uart_server.py`** reads and writes the consoles directly over
ser2net (one persistent connection per port, background logging to
`/tmp/uart-logs/`). It uses the same `requirements.txt` venv. Every tool takes
a port as `5`, `ttyUSB5` or `3005`; names are optional aliases in
`mcp/uart/aliases.json` (gitignored; see `aliases.example.json`), re-read on
every call and settable with the `name_uart` tool. Match `UART_HOST`,
`UART_BASE_PORT` and `UART_COUNT` to the panel's `RELAY_PANEL_UART_*`.

**`mcp/usb-hotplug/server.ts`** runs `udevadm monitor` on the Pi over ssh and
pushes each USB add/remove as a channel notification, with `usb_wait` to block
until a device (say the Ingenic bootrom, `a108:c309`) appears. The push
notifications are a Claude Code channel, which a non-plugin server only gets
when Claude Code starts with
`--dangerously-load-development-channels server:usb-hotplug`; without it the
tools (`usb_wait`, `usb_events`, `usb_devices`) still work. It needs
[Bun](https://bun.sh) and key-based ssh to `USB_HOTPLUG_HOST`:

```bash
bun install --cwd mcp/usb-hotplug
```

```json
{"mcpServers": {
  "uart": {
    "command": "/path/to/relay-panel/.venv/bin/python",
    "args": ["/path/to/relay-panel/mcp/uart/uart_server.py"],
    "env": {"UART_HOST": "your-pi", "UART_BASE_PORT": "3000", "UART_COUNT": "24",
            "RELAY_PANEL_URL": "http://your-pi:8080"}},
  "usb-hotplug": {
    "command": "bun",
    "args": ["run", "/path/to/relay-panel/mcp/usb-hotplug/server.ts"],
    "env": {"USB_HOTPLUG_HOST": "root@your-pi"}}}}
```

## Skills and the bench map

`skills/` holds Claude Code skills that drive a whole flash or boot cycle
through the three MCP servers. They know nothing about a particular bench:
each looks the device up in `bench.toml` through `tools/bench.py` (Python
3.11+).

```bash
cp bench.example.toml bench.toml      # then describe your devices
tools/bench.py check                  # compare it with the live panel
mkdir -p ~/.claude/skills && ln -s "$PWD"/skills/* ~/.claude/skills/
```

Link them, do not copy: each skill finds `tools/bench.py` (and `/bootstrap`
reuses `/flash`'s steps) through its real location in this repo.

| Skill | Does |
|---|---|
| `/flash <device> <image>` | bootrom, DFU write, cold boot (`/flash cloner ...` for the legacy path) |
| `/remote-flash <device> <image>` | same as `/flash` with DFU |
| `/bootstrap <device>` | bootrom into U-Boot DFU, no write |
| `/poweroff <device\|all>` | power and boot pin off |
| `/uart ...` | console logs, rotation, U-Boot break-in |
| `/capture start\|stop` | usbmon capture on the Pi |
| `/deploy-dfu [checkout]` | build and install `dfu-remote` on the Pi |

Every skill works the same for a camera on a backpack: `bench.toml` says which,
and the skill uses the unit's `bootrom` action and DFU address instead of relays.

[docs/bench.md](docs/bench.md) covers the boot sequence, development loops
and bench habits.

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
