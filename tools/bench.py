#!/usr/bin/env python3
"""The bench's device map, as the panel holds it, for the skills and for people.

  bench.py list              devices: relays, console or backpack, notes
  bench.py show <device>     one device as JSON, bench defaults filled in
                             (a backpack device: its unit's dfu address and console name)
  bench.py bench             the bench-wide settings from bench.toml, as JSON
  bench.py check             each device against the live panel: relay names, rails, backpacks
  bench.py push              send bench.toml's [devices.*] to the panel (adds new names,
                             updates existing ones); the panel's map is the one used after

bench.toml ($BENCH_TOML, else bench.toml at the repo root) holds the panel's
address and bench-wide settings. Devices live in the panel.
"""
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PATH = os.environ.get("BENCH_TOML", os.path.join(ROOT, "bench.toml"))
INHERITED = ("settle_secs", "enumerate_timeout_secs")
DEFAULTS = {"settle_secs": 3, "enumerate_timeout_secs": 90,
            "bootrom_vid_pid": "a108:c309", "master_name_contains": "master",
            "dfu_dir": "/root/thingino-dfu-linux-aarch64"}
BENCH_KEYS = ("panel_url", "dfu_host", "dfu_dir", "ssh", "bootrom_vid_pid",
              "usbmon_bus", "master_name_contains")


def load():
    try:
        with open(PATH, "rb") as f:
            bench = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"no bench file at {PATH}: copy bench.example.toml to bench.toml")
    if "panel_url" not in bench:
        sys.exit(f"{PATH} has no panel_url")
    for k, v in DEFAULTS.items():
        bench.setdefault(k, v)
    bench.setdefault("devices", {})
    return bench


def panel(bench, path, body=None, method=None):
    url = bench["panel_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            sys.exit(f"{url}: HTTP {e.code}")
    except (OSError, ValueError) as e:
        sys.exit(f"cannot reach the panel at {url}: {e}")


def devices(bench):
    found = panel(bench, "/api/devices").get("devices", [])
    if not found and bench["devices"]:
        print(f"note: the panel has no devices yet; using {PATH}'s. Run `bench.py push`.", file=sys.stderr)
        return [dict(d, name=name) for name, d in bench["devices"].items()]
    return found


def relay(r):
    return f"B{r['board']}/{r['relay']}" if r else "-"


def find_backpack(units, ref):
    return next((u for u in units if str(ref) in (str(u["id"]), u["name"], u.get("mdns") or "", u["host"])), None)


def console_name(d):
    uart = d.get("uart")
    if d.get("uart_name"):
        return d["uart_name"]
    if isinstance(uart, int):
        return f"ttyUSB{uart}"
    return uart


def find(bench, name):
    known = devices(bench)
    match = next((d for d in known if d["name"].lower() == name.lower()), None)
    if match is None:
        sys.exit(f"unknown device '{name}'; known: {', '.join(d['name'] for d in known)}")
    return match


def show(bench, name):
    d = find(bench, name)
    out = {k: bench[k] for k in INHERITED}
    out.update({k: v for k, v in d.items() if k not in ("uart_name",)})
    if "usb_mux" in out:
        out["usb_mux"].setdefault("host_state", "off")
    for k in BENCH_KEYS:
        if k in bench:
            out[k] = bench[k]
    if "backpack" in out:
        unit = find_backpack(panel(bench, "/api/backpacks")["backpacks"], out["backpack"])
        if unit is None:
            sys.exit(f"{name}: no backpack {out['backpack']!r} on the panel")
        # The unit is the flashing host and the console, not the Pi.
        out.update(backpack_id=unit["id"], backpack=unit["name"], backpack_name=unit["name"],
                   online=unit["online"], dfu_host=unit["dfu"], uart=unit["name"])
    elif out.get("uart") is not None:
        out["uart"] = console_name(d)
    return out


def cmd_list(bench):
    print(f"{'device':<14} {'power':<7} {'boot':<7} {'console':<16} notes")
    for d in devices(bench):
        if "backpack" in d:
            where = f"backpack {d.get('uart_name') or d['backpack']}"
            print(f"{d['name']:<14} {where:<32} {d.get('notes', '')}")
            continue
        print(f"{d['name']:<14} {relay(d.get('power')):<7} {relay(d.get('boot')):<7} "
              f"{console_name(d) or '-':<16} {d.get('notes', '')}")


def cmd_check(bench):
    state = panel(bench, "/api/state")
    boards = state["boards"]
    units = state.get("backpacks", [])
    master = bench["master_name_contains"].lower()
    bad = 0
    for d in devices(bench):
        name = d["name"]
        if "backpack" in d:
            unit = find_backpack(units, d["backpack"])
            if unit is None:
                print(f"{name}: no backpack {d['backpack']!r} on the panel")
                bad += 1
            else:
                state_text = "online" if unit["online"] else f"OFFLINE ({unit['error']})"
                print(f"  {name:<14} backpack {unit['name']} #{unit['id']}, {state_text}")
            continue
        for role in ("power", "boot", "detach"):
            r = d.get(role)
            if not r:
                continue
            try:
                label = boards[r["board"]]["relays"][r["relay"]].get("name", "")
            except (IndexError, KeyError):
                print(f"{name}: {role} {relay(r)} does not exist on the panel")
                bad += 1
                continue
            if master in label.lower():
                print(f"{name}: {role} {relay(r)} is the rail '{label}'")
                bad += 1
            print(f"  {name:<14} {role:<7} {relay(r):<7} panel: {label}")
    print("OK" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def cmd_push(bench):
    if not bench["devices"]:
        sys.exit(f"{PATH} has no [devices.*] to push")
    items = [dict(d, name=name) for name, d in bench["devices"].items()]
    answer = panel(bench, "/api/devices/import", {"devices": items})
    if answer.get("error"):
        for line in answer.get("errors", [answer["error"]]):
            print(f"refused: {line}", file=sys.stderr)
        sys.exit("nothing was changed on the panel")
    for row in answer["devices"]:
        print(f"  {row['result']:<8} {row['name']} (#{row['id']})")
    print(f"The panel now holds the device map; the [devices.*] tables in {PATH} can go.")


def main():
    args = sys.argv[1:]
    bench = load()
    if args[:1] == ["bench"]:
        print(json.dumps({k: v for k, v in bench.items() if k != "devices"}, indent=2))
    elif args[:1] == ["list"]:
        cmd_list(bench)
    elif args[:1] == ["show"] and len(args) == 2:
        print(json.dumps(show(bench, args[1]), indent=2))
    elif args[:1] == ["check"]:
        sys.exit(cmd_check(bench))
    elif args[:1] == ["push"]:
        cmd_push(bench)
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main()
