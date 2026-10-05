# Relay Control Panel API

The examples below use `$PANEL` for the panel's base URL. Set it once:

```bash
export PANEL=http://your-pi:8080
```

Requests and responses use JSON, except for the UART event stream below.
Set `Content-Type: application/json` on POST/PUT.

JSON responses include `server_id`, an opaque identifier that changes when the
server restarts. Successful writes also include `write_seq`. Compare counters
only within the same `server_id`; discard late responses from an older process.

Board addresses, bus numbers and channel assignments in the examples are
illustrative — they describe whatever your bench is wired as, not a fixed
layout.

## Get State

Get all boards, relay names, and on/off states.

```bash
curl -s $PANEL/api/state
```

Response:
```json
{
  "boards": [
    {
      "index": 0,
      "name": "Board 1",
      "address": "0x20",
      "bus": 2,
      "size": 2,
      "layout": "grid",
      "flipped": false,
      "relays": [
        {"num": 0, "name": "Relay 0", "state": "off"},
        {"num": 1, "name": "Relay 1", "state": "on"}
      ],
      "error": null
    }
  ],
  "gpios": [],
  "pwm": [],
  "usb": [],
  "usb_vid": "a108",
  "server_id": "opaque-process-id",
  "sample_seq": 42,
  "covers_writes": 7,
  "sampled_age_s": 0.12,
  "stale": false
}
```

State requests normally share a hardware snapshot. Use `/api/state?fresh=1` for
an immediate read. `sample_seq` orders observations by when their sweeps began;
an older sweep cannot replace a newer published observation. `covers_writes`
identifies writes completed before that sweep began. A client confirming write
7 should wait for `covers_writes >= 7` from the same server process, with a
non-stale snapshot and no error on the affected device.

Treat device states as unverified when the device has an `error`, or when the
snapshot is `stale`. A failed read may contain placeholder OFF/zero values;
these do not prove the hardware is off. Preserve a clearly marked last known
reading or show an unknown state. A timed-out write may have reached the
hardware: reconcile with a new reading instead of automatically retrying a
toggle.

## UART consoles

### List available UARTs

```bash
curl -s "$PANEL/api/uarts"
```

Returns the UARTs the caller may view. Listing does not open any upstream
connections. `input` says whether the caller may also type into that UART, and
`read_only` is true when it may type into none of them. Example with two
configured UARTs, for a token granted input on `ttyUSB0` only:

```json
{
  "uarts": [
    {"index": 0, "name": "ttyUSB0", "port": 3000, "input": true},
    {"index": 1, "name": "ttyUSB1", "port": 3001, "input": false}
  ],
  "read_only": false,
  "server_id": "opaque-process-id"
}
```

Defaults are 24 UARTs at `127.0.0.1:3000` through `:3023`, matching
`uart-monitor`. Operators can set `RELAY_PANEL_UART_HOST`,
`RELAY_PANEL_UART_BASE_PORT`, and `RELAY_PANEL_UART_COUNT` (0–64) on the server.
Callers cannot supply arbitrary hostnames or TCP ports.

### Stream output

```bash
curl -N "$PANEL/api/uart/stream?uart=0"
curl -N "$PANEL/api/uart/stream?uart=all"
```

This is a long-lived `text/event-stream` response. `uart=all` combines all
UARTs the caller may view into one response. Example event frames:

```text
event: ready
data: {"uarts": [0], "server_id": "opaque-process-id"}

event: status
data: {"uart": 0, "status": "connected"}

event: output
data: {"uart": 0, "text": "Booting...\r\n"}

```

| Event | Data and meaning |
|---|---|
| `ready` | Selected UART indices and server ID |
| `status` | `uart`, `status`: `connecting`, `connected`, or `disconnected`; an optional `message` describes a connection failure |
| `output` | `uart` and UTF-8 decoded `text`; chunks may contain partial or multiple lines and terminal escapes |
| `gap` | `message`: a slow client exceeded its bounded queue; some output was dropped and the stream closes |
| `denied` | `message`: access was revoked; the stream closes |

Comment heartbeats arrive every five seconds, even when the serial device is
quiet. Upstream TCP failures retry automatically; clients should reconnect if
the HTTP stream closes, except after an access denial. There is no history
replay or `Last-Event-ID` recovery. Invalid UTF-8 is replaced with `�`.

The stream itself never sends bytes to the UART; only
[`POST /api/uart/input`](#send-input) does. Browser subscribers share one raw
TCP connection per selected UART, which closes when its last viewer leaves.
Each HTTP viewer has a bounded queue, with a maximum of 32 concurrent viewers.
Clients must parse JSON text and render it as text, not HTML.

Invalid selections return 400, access failures 403, and a full viewer limit
503 before starting the stream. Both UART endpoints require a trusted source
or an authorized bearer token. Scoped tokens need `uarts` in their `allowed`
map; `all` includes only those granted indices. For example, a console-only
entry in the server's `tokens.json`:

```json
{"tokens": {"<sha256-of-token>": {"allowed": {"uarts": [0, 1]}}}}
```

Send the token in the header, including for the stream:

```bash
curl -N -H "Authorization: Bearer $TOKEN" "$PANEL/api/uart/stream?uart=all"
```

Open streams recheck permission every five seconds. The UI uses streaming
`fetch` so the bearer token stays in the header rather than the URL.

### Send input

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"uart": 0, "data": "printenv\r"}' "$PANEL/api/uart/input"
```

Writes `data`, UTF-8 encoded, to the UART exactly as given: no line ending is
added, so send `\r` for Enter and `\u0003` for Ctrl-C. At most 4096 bytes per
request. Returns `{"ok": true, "uart": 0, "bytes": 9}`.

Input travels over the upstream connection that viewers share, so the UART must
have at least one open stream: input nobody can see the answer to is refused
with 409 rather than opening a connection for it. ser2net passes bytes from any
of its clients to the tty, so this mixes with anything else connected to the
same port. Send one request at a time to keep keystrokes in order; the UI queues
them and drops whatever is still queued after a failure instead of retrying.

The request must be `Content-Type: application/json`, and a browser `Origin`
must match the panel's own host (403 otherwise), so another site cannot type
into a bench device. Input needs the same trusted source or token as viewing,
plus, for scoped tokens, a `uart_input` grant for that index, effective only
alongside `uarts`:

```json
{"tokens": {"<sha256-of-token>": {"allowed": {"uarts": [0, 1], "uart_input": [0]}}}}
```

| Status | Meaning |
|---|---|
| 400 | Not a JSON object, invalid `uart`, or `data` not a non-empty string |
| 403 | No input grant for this UART, or a foreign `Origin` |
| 409 | No open stream for this UART, so no upstream connection |
| 413 | `data` over 4096 bytes once encoded |
| 415 | Content type is not `application/json` |
| 503 | The upstream write failed |

Input does not count as a bench write: the response carries no `write_seq` and
triggers no hardware resample.

## Control Relays

### Toggle a relay

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5}' \
  $PANEL/api/relay/toggle
```

Response includes `{"relay": 5, "state": "on", "board": 0}` plus `name`,
`server_id`, and `write_seq`.

### Set a relay on or off

```bash
# Turn on
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5,"state":true}' \
  $PANEL/api/relay/set

# Turn off
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5,"state":false}' \
  $PANEL/api/relay/set
```

Request `state` is a boolean. The strings `"on"`/`"off"` (also
`true`/`false`, `1`/`0`, `yes`/`no`) are accepted too; anything else is a 400
rather than a guess, because `bool("off")` is true and guessing once meant
switching a relay on when asked to switch it off.

Response: `{"relay": 5, "name": "...", "state": "on", "board": 0}` - note the
state comes back as the string `"on"` or `"off"`, not as the boolean you sent.

### All on / all off (per board)

```bash
# All on
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"state":true}' \
  $PANEL/api/board/all

# All off
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"state":false}' \
  $PANEL/api/board/all
```

Response: `{"ok": true}`

## Naming

### Rename a relay

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5,"name":"Front Light"}' \
  $PANEL/api/relay/name
```

Response: `{"ok": true}`

### Rename a board

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"name":"Bench Rack"}' \
  $PANEL/api/board/name
```

Response: `{"ok": true}`

## Board Management

### Add a board

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"name":"Board 2","address":"0x21","bus":2,"size":8}' \
  $PANEL/api/boards
```

Response: `{"ok": true}`

Sizes: 1, 2, 4, 8, 16. Addresses: 0x20-0x27.

### Remove a board

```bash
curl -s -X DELETE $PANEL/api/boards/1
```

The number is the board index (0-based).

Response: `{"ok": true}`

### Change board settings (address, bus, size)

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"address":"0x22","bus":2,"size":16}' \
  $PANEL/api/board/settings
```

Response: `{"ok": true}`

## Display Settings

### Toggle layout (grid / PCB view)

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"layout":"board"}' \
  $PANEL/api/board/layout
```

Values: `"grid"` or `"board"`. Response: `{"ok": true}`

### Toggle flip (reverse relay order)

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0}' \
  $PANEL/api/board/flip
```

Response: `{"flipped": true}`

## GPIO Pins (direct HI/LOW control)

GPIOs are individual wiringPi-numbered pins (see `gpio readall`) used for
direct on/off control of arbitrary loads — e.g. a USB switch on physical pin 40
(wPi 27). Each configured GPIO is set to output mode when added and its last
known state is restored on server startup.

### Get GPIOs only

```bash
curl -s $PANEL/api/gpios
```

Response:
```json
{
  "gpios": [
    {"wpi": 27, "pin": 40, "name": "USB Switch", "state": "on", "error": null}
  ]
}
```

GPIOs are also included in `GET /api/state` under the `gpios` key.

### Add a GPIO

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"wpi":27,"pin":40,"name":"USB Switch"}' \
  $PANEL/api/gpios
```

`wpi` is required; `pin` and `name` are optional. Starts in OFF state and sets
`gpio mode <wpi> out`.

Response: `{"ok": true}`

### Remove a GPIO

```bash
curl -s -X DELETE $PANEL/api/gpios/27
```

The path parameter is the wPi number. Does not change the pin state on the hardware.

Response: `{"ok": true}`

### Toggle a GPIO

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"wpi":27}' \
  $PANEL/api/gpio/toggle
```

Response: `{"wpi": 27, "name": "USB Switch", "state": "on"}`

### Set a GPIO on or off

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"wpi":27,"state":true}' \
  $PANEL/api/gpio/set
```

Response: `{"wpi": 27, "name": "USB Switch", "state": "on"}`

### Rename a GPIO

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"wpi":27,"name":"Front USB"}' \
  $PANEL/api/gpio/name
```

Response: `{"ok": true}`

### Update physical pin label

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"wpi":27,"pin":40}' \
  $PANEL/api/gpio/pin
```

Response: `{"ok": true}`

## USB Boot Devices (read-only)

Live list of connected USB devices with Ingenic's vendor ID `a108` — the
mask-ROM bootrom (`a108:c309`) and U-Boot's DFU gadget (`a108:4d44`). Read
fresh from `/sys/bus/usb/devices` on every request, so devices appear and
disappear as they physically enumerate. Also shown live at the bottom of
the web UI (1 s poll).

```bash
curl -s $PANEL/api/usb
```

Response:
```json
{
  "usb": [
    {
      "port": "4-1.1.1.2.1",
      "vid": "a108",
      "pid": "c309",
      "product": "USB Boot Device",
      "manufacturer": "Ingenic",
      "serial": null,
      "busnum": "4",
      "devnum": "71"
    }
  ]
}
```

`port` is the sysfs bus-port path (stable per physical socket). String
descriptors the device does not report come back as `null` (the bootrom has
no serial). The same list is included in `GET /api/state` under the `usb` key.

## PWM / LED Dimming (PCA9685)

Continuous brightness control for 12 V LED strips driven through MOSFET
modules, used to simulate day/night transitions for camera testing. Addressed
by chip index + channel number, and set with a **level** in counts (0-4095) or
a **percent** (0-100). Levels are read back from the chip on every request, so
the API reports what the hardware is doing rather than what was last asked for.

Frequency is a property of the whole chip, not per channel. The PCA9685 divides
a 25 MHz oscillator by an integer `PRE_SCALE`, giving **24 Hz to 1526 Hz** at a
full 12 bits of duty resolution at every frequency. The steps are uneven and
coarse at the top, so the achieved frequency differs from the requested one;
every response reports the achieved value.

### Get PWM state

```bash
curl -s $PANEL/api/pwm
```

Response:
```json
{
  "pwm": [
    {
      "index": 0,
      "name": "LED Strips",
      "address": "0x40",
      "bus": 2,
      "size": 4,
      "prescale": 3,
      "freq_hz": 1525.9,
      "channels": [
        {"ch": 0, "name": "White Strip", "phase": 0,    "level": 2048, "percent": 50.01},
        {"ch": 1, "name": "RGB Red",     "phase": 1024, "level": 0,    "percent": 0.0}
      ],
      "error": null
    }
  ]
}
```

Also included in `GET /api/state` under the `pwm` key.

`phase` is the count at which the channel turns on within the 4096-step cycle.
Channels lit together default to a quarter-period stagger (0, 1024, 2048, 3072)
so their on-times spread across the cycle instead of stacking at count 0, which
lowers both peak current draw and combined optical ripple.

### Set a channel

```bash
# by raw duty - use this for repeatable test steps
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":0,"level":2048}' \
  $PANEL/api/pwm/set

# by percent
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":0,"percent":50}' \
  $PANEL/api/pwm/set
```

Response: `{"chip":0,"channel":0,"name":"White Strip","level":2048,"percent":50.01}`

Level 0 and level 4095 use the chip's FULL_OFF / FULL_ON flags, so the extremes
are a genuinely constant rail rather than a 160 ns sliver of a pulse.

### Set every channel on a chip

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"level":4095}' \
  $PANEL/api/pwm/all
```

Each channel keeps its own stagger phase.

### All off

```bash
# one chip
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0}' $PANEL/api/pwm/off

# every configured chip
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{}' $PANEL/api/pwm/off
```

Single I2C write to the ALL_LED registers, so all 16 channels go dark together
regardless of the configured `size`.

### Channel groups

A group is a named set of channels on one chip driven by a single master
level - e.g. the R, G and B legs of one strip acting as a single light, so you
can dim the whole strip without touching the white strip on another channel.

Members carry a **weight** (0-4095, default full) which is the level they sit
at when the master is full. Equal weights mean the master just sets every
member the same. Unequal weights make the master scale them proportionally,
which is what holds a calibrated white point as an RGB strip dims.

The master is **derived from the hardware**, not stored, referenced to the
member with the largest weight. So a channel nudged on its own cannot leave a
stale master behind, and dragging back up from zero still recovers the balance
because the ratios live in the weights. When a member sits off its weighted
ratio the group reports `"mixed": true`.

Groups appear per chip in `GET /api/pwm`:

```json
"groups": [
  {
    "index": 0,
    "name": "RGB Strip",
    "channels": [1, 2, 3],
    "weights": {"1": 4095, "2": 4095, "3": 4095},
    "level": 2048,
    "percent": 50.01,
    "mixed": false
  }
]
```

```bash
# create
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"name":"RGB Strip","channels":[1,2,3]}' \
  $PANEL/api/pwm/groups

# drive every member from one master
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"group":0,"level":2048}' \
  $PANEL/api/pwm/group

# rename, re-scope, or set weights (any subset of the three)
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"group":0,"name":"RGB Strip","channels":[1,2,3],
       "weights":{"1":4095,"2":3200,"3":2600}}' \
  $PANEL/api/pwm/group

# delete
curl -s -X DELETE $PANEL/api/pwm/groups/0/0
```

Setting a group responds with the exact member levels written:
`{"chip":0,"group":0,"name":"RGB Strip","level":2048,"percent":50.01,
"channels":{"1":2048,"2":2048,"3":2048}}`

### Set frequency

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"freq_hz":1526}' \
  $PANEL/api/pwm/freq
```

Response: `{"chip":0,"requested_hz":1526,"prescale":3,"freq_hz":1525.9}`

Clamped to 24-1526 Hz. Changing it briefly sleeps the chip (outputs drop for
about a millisecond) and then restarts the channels at their prior duty.

### Set stagger phase

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":2,"phase":1365}' \
  $PANEL/api/pwm/phase
```

Response: `{"ok": true, "phase": 1365}`. Re-applies at the current duty.

### Rename

```bash
# a channel
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":0,"name":"White Strip"}' \
  $PANEL/api/pwm/name

# the chip itself (omit `channel`)
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"name":"LED Strips"}' \
  $PANEL/api/pwm/name
```

### Add / remove / reset a chip

```bash
# `size` seeds that many channels (0..size-1); add or remove them individually after
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"name":"LED Strips","address":"0x40","bus":2,"size":4,"freq_hz":1526}' \
  $PANEL/api/pwm

curl -s -X DELETE $PANEL/api/pwm/0

# re-init: wake, auto-increment on, re-apply frequency, all channels off
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0}' $PANEL/api/pwm/reset
```

Removing a chip blanks its outputs first. Adding is refused if the address is
already claimed on that bus, **including by a relay board** - a PCA9685 write
aimed at an XL9535 lands on its output registers and power-cycles whatever is
plugged into it.

### Change I2C address or bus

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"address":"0x41","bus":2}' \
  $PANEL/api/pwm/settings
```

Response: `{"ok":true,"address":"0x41","bus":2,"moved":true}`

Same conflict check as adding. When the chip actually moves, the old address is
blanked first so nothing is left lit on a chip no longer being managed, then
the new one is initialised (wake, auto-increment, frequency, all channels off).
A 500 means nothing answered at the new address.

### Add / remove individual channels

Which channels a chip manages is driven by the `channels` map, not a count, so
they need not be contiguous - manage 0-3 for two strips and 7 for an IR bank
with nothing in between. `GET /api/pwm` reports `available`, the channels the
chip has that this config does not yet manage.

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":7,"name":"IR 850nm"}' \
  $PANEL/api/pwm/channels

curl -s -X DELETE $PANEL/api/pwm/channels/0/7
```

A newly added channel starts dark. Removing one blanks it and drops it from
any group; a group left with no members is removed too and named in the
response's `groups_dropped`.

Configs written before per-channel control used a `size` field. Those still
work and are expanded into an explicit map the first time a channel is added
or removed.

## Errors

All errors return HTTP 400 with:
```json
{"error": "description of the problem"}
```

## Notes

- `board` is always the 0-based board index
- `relay` is the relay number (0-15 for a 16ch board)
- `wpi` is the wiringPi pin number (from `gpio readall`)
- All state changes take effect immediately on the hardware (via `i2cset` for
  relays, `gpio write` for GPIOs)
- `chip` is the 0-based index into the `pwm` list; `channel` is 0-15
- Config persists in `/opt/relay-panel/config.json`
- GPIO state is restored on server start (mode out + last value)
- **Relays and PWM channels are forced OFF on server start.** A restart is a
  known-safe state, not a resume. No duty is persisted across a restart on
  purpose, so a stale config can never bring a strip back up at an unexpected
  level. Snapshot what is on before restarting the service and re-set it after.
- Web UI at `$PANEL`
