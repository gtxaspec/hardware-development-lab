#!/usr/bin/env python3
"""Read the bench map (bench.toml) for the skills and for people.

  bench.py list              device names, relays and notes
  bench.py show <device>     one device as JSON, bench defaults filled in
  bench.py bench             the bench-wide settings as JSON
  bench.py check             compare the file with the live panel

The file is $BENCH_TOML, else bench.toml at the repo root.
"""
import json
import os
import sys
import tomllib
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PATH = os.environ.get("BENCH_TOML", os.path.join(ROOT, "bench.toml"))
INHERITED = ("settle_secs", "enumerate_timeout_secs")
DEFAULTS = {"settle_secs": 3, "enumerate_timeout_secs": 90,
            "bootrom_vid_pid": "a108:c309", "master_name_contains": "master",
            "dfu_dir": "/root/thingino-dfu-linux-aarch64"}


def load():
    try:
        with open(PATH, "rb") as f:
            bench = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"no bench file at {PATH}: copy bench.example.toml to bench.toml")
    for k, v in DEFAULTS.items():
        bench.setdefault(k, v)
    bench.setdefault("devices", {})
    return bench


def device(bench, name):
    dev = bench["devices"].get(name)
    if dev is None:
        sys.exit(f"unknown device '{name}'; known: {', '.join(bench['devices'])}")
    out = {"name": name}
    out.update({k: bench[k] for k in INHERITED})
    out.update(dev)
    if "usb_mux" in out:
        out["usb_mux"].setdefault("host_state", "off")
    for k in ("panel_url", "dfu_host", "dfu_dir", "ssh", "bootrom_vid_pid",
              "usbmon_bus", "master_name_contains"):
        if k in bench:
            out[k] = bench[k]
    return out


def relay(r):
    return f"B{r['board']}/{r['relay']}" if r else "-"


def cmd_list(bench):
    print(f"{'device':<14} {'power':<7} {'boot':<7} {'uart':<10} notes")
    for name, d in bench["devices"].items():
        print(f"{name:<14} {relay(d.get('power')):<7} {relay(d.get('boot')):<7} "
              f"{str(d.get('uart', '-')):<10} {d.get('notes', '')}")


def cmd_check(bench):
    with urllib.request.urlopen(bench["panel_url"].rstrip("/") + "/api/state", timeout=5) as r:
        state = json.load(r)
    boards = state["boards"]
    master = bench["master_name_contains"].lower()
    used, bad = {}, 0
    for name, d in bench["devices"].items():
        for role in ("power", "boot", "detach"):
            r = d.get(role)
            if not r:
                continue
            key = (r["board"], r["relay"])
            try:
                label = boards[r["board"]]["relays"][r["relay"]].get("name", "")
            except (IndexError, KeyError):
                print(f"{name}: {role} {relay(r)} does not exist on the panel")
                bad += 1
                continue
            if master in label.lower():
                print(f"{name}: {role} {relay(r)} is the rail '{label}'")
                bad += 1
            if key in used:
                print(f"{name}: {role} {relay(r)} also used by {used[key]}")
                bad += 1
            used[key] = f"{name}.{role}"
            print(f"  {name:<14} {role:<7} {relay(r):<7} panel: {label}")
    print("OK" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def main():
    args = sys.argv[1:]
    bench = load()
    if args[:1] == ["bench"]:
        print(json.dumps({k: v for k, v in bench.items() if k != "devices"}, indent=2))
    elif args[:1] == ["list"]:
        cmd_list(bench)
    elif args[:1] == ["show"] and len(args) == 2:
        print(json.dumps(device(bench, args[1]), indent=2))
    elif args[:1] == ["check"]:
        sys.exit(cmd_check(bench))
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main()
