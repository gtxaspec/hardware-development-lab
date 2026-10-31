---
name: capture
description: Start or stop a USB packet capture on the bench Pi
user-invocable: true
---

Capture USB traffic on the Pi with tcpdump on usbmon, for debugging the
bootrom, DFU or cloner protocols.

Usage:
- `/capture start [name]` - default name `capture_YYYYMMDD_HHMMSS`
- `/capture stop [dest_dir]` - stop and copy the pcap here (default `./captures/`)

Settings come from `<repo>/tools/bench.py bench` (this skill lives at
`<repo>/skills/capture/`; resolve symlinks with `readlink -f`): `ssh` is the
Pi, `usbmon_bus` the bus the devices are on (`lsusb` on the Pi shows it).

Start:
1. `ssh -n <ssh> "rm -f /tmp/<name>.pcap; modprobe usbmon; setsid tcpdump -i usbmon<usbmon_bus> -w /tmp/<name>.pcap </dev/null >/dev/null 2>&1 &"`
2. Verify after a second: `ssh -n <ssh> "pgrep -a tcpdump"`.

Stop:
1. `ssh -n <ssh> "pkill -x tcpdump"`, wait a second.
2. `mkdir -p <dest_dir> && scp <ssh>:/tmp/<name>.pcap <dest_dir>/`
3. Report the file and its size.
