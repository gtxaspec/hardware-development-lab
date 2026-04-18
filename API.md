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

## Errors

All errors return HTTP 400 with:
```json
{"error": "description of the problem"}
```

## Notes

- `board` is always the 0-based board index
- `relay` is the relay number (0-15 for a 16ch board)
- All state changes take effect immediately on the hardware via i2c
- Config persists in `/opt/relay-panel/config.json`
- Web UI at `http://your-pi:8080`
