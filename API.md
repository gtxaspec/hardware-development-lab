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
- Config persists in `/opt/relay-panel/config.json`
- GPIO state is restored on server start (mode out + last value)
- Web UI at `http://your-pi:8080`
