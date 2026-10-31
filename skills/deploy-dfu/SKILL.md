---
name: deploy-dfu
description: Build thingino-dfu for arm64 and deploy the dfu-remote daemon to the bench Pi
user-invocable: true
---

Build thingino-dfu for aarch64, install the `dfu-remote` daemon, the
`thingino-dfu` CLI and the firmware loader tree on the Pi, and restart the
daemon. Optionally refresh the client on this host.

Usage: `/deploy-dfu [path to thingino-dfu checkout]` (default `$THINGINO_DFU_DIR`)

Settings from `<repo>/tools/bench.py bench` (this skill lives at
`<repo>/skills/deploy-dfu/`; resolve symlinks with `readlink -f`): `ssh`,
`dfu_host`, `dfu_dir`.

Do not deploy while something is flashing: check
`ssh -n <ssh> "ss -tn state established '( sport = :5050 )'"` is empty and no
DFU gadget is enumerated.

Steps:
1. In the checkout: `make arm64`.
2. Copy:
   ```bash
   ssh -n <ssh> "mkdir -p <dfu_dir>"
   scp build-aarch64/cli/thingino-dfu build-aarch64/dfu-remote/dfu-remote <ssh>:<dfu_dir>/
   rsync -a --delete firmware/ <ssh>:<dfu_dir>/firmware/
   ```
   The loader tree is a local copy on the Pi; redeploy whenever the
   checkout's `firmware/` changes.
3. Restart. If `ssh -n <ssh> "systemctl is-enabled dfu-remote"` says enabled:
   `ssh -n <ssh> "systemctl restart dfu-remote"`. Otherwise:
   ```bash
   ssh -n <ssh> "pkill -x dfu-remote; cd <dfu_dir> && setsid ./dfu-remote -p 5050 --firmware-dir <dfu_dir>/firmware </dev/null >/tmp/dfu-remote.log 2>&1 &"
   ```
4. Verify: `thingino-dfu --host <dfu_host> -l` answers.
5. Optional: `sudo make install` in the checkout so this host's client matches.
6. Report.
