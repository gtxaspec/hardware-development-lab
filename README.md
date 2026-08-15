# Relay Control Panel

Web panel, HTTP API and MCP server for the camera test bench: I²C relay boards,
direct GPIO pins, dimmable LED strips, and a live view of which devices are
sitting in a flashable USB state.

It runs on a single-board computer wired into the bench — an Orange Pi here, but
anything with I²C and a `gpio` CLI will do — and serves on port 8080.

## What it drives

| | Hardware | Addressing | Control |
|---|---|---|---|
| **Relays** | XL9535 16-channel I²C boards | bus + address `0x20`–`0x27` | on / off / toggle, per board or all |
| **GPIOs** | Orange Pi header pins via `wiringOP` | wiringPi number | on / off / toggle |
| **LED PWM** | PCA9685 16-channel, driving 12 V strips through MOSFET modules | bus + address, typically `0x40` | 12-bit duty per channel, 24–1526 Hz, channel groups |
| **USB** | Ingenic bootrom / U-Boot DFU gadget | read-only | shows what is flashable right now |

The PWM section exists to simulate day/night transitions for camera testing:
continuous, repeatable light levels rather than the relay-binary room light the
bench had before.

## Layout

```
relay-panel.py       the server: HTTP API + hardware access. Pure stdlib.
static/index.html    the whole web UI, single file, no build step
mcp-server.py        MCP server exposing the API as tools (30 of them)
deploy.py            state-preserving deploy to the Orange Pi
test-pwm.py          pure-logic tests, no hardware, run anywhere
test-hardware.py     hardware tests, drives the real PCA9685
relay-panel.service  systemd unit
API.md               full HTTP API reference
```

`relay-panel.py` deliberately has **no third-party dependencies**. It shells out
to `i2cget`/`i2cset`/`i2cdump` and the `gpio` CLI, so the Orange Pi runs it with
the system Python and needs no venv, no pip, and no network to be provisioned.
`requirements.txt` covers `mcp-server.py` only, which runs on the dev machine.

State is never cached: every request reads the hardware back, so the panel shows
what the bench is actually doing rather than what it was last told.

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

On the Pi, enable I²C and install `i2c-tools` (for `i2cget`/`i2cset`/`i2cdump`)
and `wiringOP` (for the `gpio` CLI — only needed if you want the GPIO section).
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
scp relay-panel.py $PI:/opt/relay-panel/
scp static/index.html $PI:/opt/relay-panel/static/
scp relay-panel.service $PI:/etc/systemd/system/
ssh $PI 'systemctl daemon-reload && systemctl enable --now relay-panel'
```

Then open `http://your-pi:8080` and add your boards, GPIOs and PWM chips from
the web UI. Nothing else needs editing — the config is built through the UI, not
checked in.

### Environment variables

All optional; the defaults suit a normal install.

| Variable | Default | Used by |
|---|---|---|
| `RELAY_PANEL_DIR` | `/opt/relay-panel` | server: install dir holding `config.json` and `static/` |
| `RELAY_PANEL_HOST` | `0.0.0.0` | server: listen address |
| `RELAY_PANEL_PORT` | `8080` | server: listen port |
| `RELAY_PANEL_USB_VID` | `a108` (Ingenic) | server: which USB vendor the USB section watches |
| `RELAY_PANEL_URL` | `http://localhost:8080` | MCP server: where the panel is |
| `RELAY_PANEL_SSH` | *(none)* | `deploy.py`: ssh target, usually set in `.env` |

`config.json` and `.env` are the only site-specific files, and both are
gitignored — nothing in version control names a particular machine.

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

**Wire a 10 kΩ pulldown on each MOSFET module's signal input.** The PCA9685's
outputs are undriven until it is configured, and without the pulldown the gates
float during boot and the strips flash on. Firmware cannot fix a state that
exists before firmware runs.

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

## Testing

```bash
python3 test-pwm.py                              # 79 pure-logic checks, no hardware
ssh root@your-pi python3 - < test-hardware.py    # 63 checks, drives the real chip
```

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

## License

MIT. See [LICENSE](LICENSE).
