# Relay Control Panel API

Base URL: `http://your-pi:8080`

All requests use JSON. Set `Content-Type: application/json` on POST/PUT.

## Get State

Get all boards, relay names, and on/off states.

```bash
curl -s http://your-pi:8080/api/state
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
      "size": 16,
      "layout": "grid",
      "flipped": false,
      "relays": [
        {"num": 0, "name": "Relay 0", "state": false},
        {"num": 1, "name": "Relay 1", "state": true},
        ...
      ],
      "error": null
    }
  ]
}
```

## Control Relays

### Toggle a relay

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5}' \
  http://your-pi:8080/api/relay/toggle
```

Response: `{"state": true}`

### Set a relay on or off

```bash
# Turn on
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5,"state":true}' \
  http://your-pi:8080/api/relay/set

# Turn off
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5,"state":false}' \
  http://your-pi:8080/api/relay/set
```

Response: `{"state": true}` or `{"state": false}`

### All on / all off (per board)

```bash
# All on
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"state":true}' \
  http://your-pi:8080/api/board/all

# All off
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"board":0,"state":false}' \
  http://your-pi:8080/api/board/all
```

Response: `{"ok": true}`

## Naming

### Rename a relay

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"relay":5,"name":"Front Light"}' \
  http://your-pi:8080/api/relay/name
```

Response: `{"ok": true}`

### Rename a board

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"name":"Bench Rack"}' \
  http://your-pi:8080/api/board/name
```

Response: `{"ok": true}`

## Board Management

### Add a board

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"name":"Board 2","address":"0x21","bus":2,"size":8}' \
  http://your-pi:8080/api/boards
```

Response: `{"ok": true}`

Sizes: 1, 2, 4, 8, 16. Addresses: 0x20-0x27.

### Remove a board

```bash
curl -s -X DELETE http://your-pi:8080/api/boards/1
```

The number is the board index (0-based).

Response: `{"ok": true}`

### Change board settings (address, bus, size)

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"address":"0x22","bus":2,"size":16}' \
  http://your-pi:8080/api/board/settings
```

Response: `{"ok": true}`

## Display Settings

### Toggle layout (grid / PCB view)

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0,"layout":"board"}' \
  http://your-pi:8080/api/board/layout
```

Values: `"grid"` or `"board"`. Response: `{"ok": true}`

### Toggle flip (reverse relay order)

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"board":0}' \
  http://your-pi:8080/api/board/flip
```

Response: `{"flipped": true}`

## GPIO Pins (direct HI/LOW control)

GPIOs are individual wiringPi-numbered pins (see `gpio readall`) used for
direct on/off control of arbitrary loads — e.g. a USB switch on physical pin 40
(wPi 27). Each configured GPIO is set to output mode when added and its last
known state is restored on server startup.

### Get GPIOs only

```bash
curl -s http://your-pi:8080/api/gpios
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
  http://your-pi:8080/api/gpios
```

`wpi` is required; `pin` and `name` are optional. Starts in OFF state and sets
`gpio mode <wpi> out`.

Response: `{"ok": true}`

### Remove a GPIO

```bash
curl -s -X DELETE http://your-pi:8080/api/gpios/27
```

The path parameter is the wPi number. Does not change the pin state on the hardware.

Response: `{"ok": true}`

### Toggle a GPIO

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"wpi":27}' \
  http://your-pi:8080/api/gpio/toggle
```

Response: `{"wpi": 27, "name": "USB Switch", "state": "on"}`

### Set a GPIO on or off

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"wpi":27,"state":true}' \
  http://your-pi:8080/api/gpio/set
```

Response: `{"wpi": 27, "name": "USB Switch", "state": "on"}`

### Rename a GPIO

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"wpi":27,"name":"Front USB"}' \
  http://your-pi:8080/api/gpio/name
```

Response: `{"ok": true}`

### Update physical pin label

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"wpi":27,"pin":40}' \
  http://your-pi:8080/api/gpio/pin
```

Response: `{"ok": true}`

## USB Boot Devices (read-only)

Live list of connected USB devices with Ingenic's vendor ID `a108` — the
mask-ROM bootrom (`a108:c309`) and U-Boot's DFU gadget (`a108:4d44`). Read
fresh from `/sys/bus/usb/devices` on every request, so devices appear and
disappear as they physically enumerate. Also shown live at the bottom of
the web UI (1 s poll).

```bash
curl -s http://your-pi:8080/api/usb
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
curl -s http://your-pi:8080/api/pwm
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
  http://your-pi:8080/api/pwm/set

# by percent
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":0,"percent":50}' \
  http://your-pi:8080/api/pwm/set
```

Response: `{"chip":0,"channel":0,"name":"White Strip","level":2048,"percent":50.01}`

Level 0 and level 4095 use the chip's FULL_OFF / FULL_ON flags, so the extremes
are a genuinely constant rail rather than a 160 ns sliver of a pulse.

### Set every channel on a chip

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"level":4095}' \
  http://your-pi:8080/api/pwm/all
```

Each channel keeps its own stagger phase.

### All off

```bash
# one chip
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0}' http://your-pi:8080/api/pwm/off

# every configured chip
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{}' http://your-pi:8080/api/pwm/off
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
  http://your-pi:8080/api/pwm/groups

# drive every member from one master
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0,"group":0,"level":2048}' \
  http://your-pi:8080/api/pwm/group

# rename, re-scope, or set weights (any subset of the three)
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"group":0,"name":"RGB Strip","channels":[1,2,3],
       "weights":{"1":4095,"2":3200,"3":2600}}' \
  http://your-pi:8080/api/pwm/group

# delete
curl -s -X DELETE http://your-pi:8080/api/pwm/groups/0/0
```

Setting a group responds with the exact member levels written:
`{"chip":0,"group":0,"name":"RGB Strip","level":2048,"percent":50.01,
"channels":{"1":2048,"2":2048,"3":2048}}`

### Set frequency

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"freq_hz":1526}' \
  http://your-pi:8080/api/pwm/freq
```

Response: `{"chip":0,"requested_hz":1526,"prescale":3,"freq_hz":1525.9}`

Clamped to 24-1526 Hz. Changing it briefly sleeps the chip (outputs drop for
about a millisecond) and then restarts the channels at their prior duty.

### Set stagger phase

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":2,"phase":1365}' \
  http://your-pi:8080/api/pwm/phase
```

Response: `{"ok": true, "phase": 1365}`. Re-applies at the current duty.

### Rename

```bash
# a channel
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"channel":0,"name":"White Strip"}' \
  http://your-pi:8080/api/pwm/name

# the chip itself (omit `channel`)
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"name":"LED Strips"}' \
  http://your-pi:8080/api/pwm/name
```

### Add / remove / reset a chip

```bash
# `size` seeds that many channels (0..size-1); add or remove them individually after
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"name":"LED Strips","address":"0x40","bus":2,"size":4,"freq_hz":1526}' \
  http://your-pi:8080/api/pwm

curl -s -X DELETE http://your-pi:8080/api/pwm/0

# re-init: wake, auto-increment on, re-apply frequency, all channels off
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"chip":0}' http://your-pi:8080/api/pwm/reset
```

Removing a chip blanks its outputs first. Adding is refused if the address is
already claimed on that bus, **including by a relay board** - a PCA9685 write
aimed at an XL9535 lands on its output registers and power-cycles whatever is
plugged into it.

### Change I2C address or bus

```bash
curl -s -X PUT -H 'Content-Type: application/json' \
  -d '{"chip":0,"address":"0x41","bus":2}' \
  http://your-pi:8080/api/pwm/settings
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
  http://your-pi:8080/api/pwm/channels

curl -s -X DELETE http://your-pi:8080/api/pwm/channels/0/7
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
- Web UI at `http://your-pi:8080`
