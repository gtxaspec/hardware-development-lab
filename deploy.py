#!/usr/bin/env python3
"""Deploy relay-panel to the Orange Pi, preserving live bench state.

Restarting the service runs init_boards() and init_pwm(), which force every
relay and every PWM channel OFF. On a bench where relays feed camera power
that is destructive if it happens mid-flash, and disruptive any other time,
so this script:

  1. refuses to run while a device is being flashed,
  2. snapshots what is on before touching anything,
  3. skips the restart entirely when only static files changed,
  4. restores the snapshot and verifies it, rather than assuming.

Usage:
    export RELAY_PANEL_SSH=root@your-pi
    ./deploy.py                 # deploy whatever changed
    ./deploy.py --host root@other-pi
    ./deploy.py --dry-run       # show what would be copied
    ./deploy.py --force         # deploy even if a flash looks active
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent
REMOTE_DIR = "/opt/relay-panel"
SERVICE = "relay-panel.service"

# local path -> remote path, and whether changing it needs a service restart.
# index.html is read from disk per request, so it never does.
FILES = [
    ("relay-panel.py", f"{REMOTE_DIR}/relay-panel.py", True),
    ("static/index.html", f"{REMOTE_DIR}/static/index.html", False),
    ("relay-panel.service", f"/etc/systemd/system/{SERVICE}", True),
]


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"FAILED: {cmd}\n{r.stderr.strip()}")
    return r.stdout.strip()


class Pi:
    def __init__(self, host):
        self.host = host

    def run(self, cmd, check=True):
        # -n matters: without it ssh consumes the caller's stdin, which
        # silently truncates any loop feeding this method
        return sh(f"ssh -n {self.host} {json.dumps(cmd)}", check)

    def api(self, method, path, body=None):
        c = f"curl -s -X {method} -H 'Content-Type: application/json'"
        if body is not None:
            c += f" -d {json.dumps(json.dumps(body))}"
        out = self.run(f"{c} http://localhost:8080{path}")
        return json.loads(out or "{}")


def local_digest(rel):
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()


def changed_files(pi):
    """Which files differ from what is already deployed."""
    out = []
    for rel, remote, needs_restart in FILES:
        got = pi.run(f"sha256sum {remote} 2>/dev/null | cut -d' ' -f1", check=False)
        if got != local_digest(rel):
            out.append((rel, remote, needs_restart))
    return out


def preflight(pi, force):
    conns = pi.run("ss -tn state established '( sport = :5050 )' | tail -n +2 | wc -l")
    dfu = [u for u in pi.api("GET", "/api/usb").get("usb", []) if u.get("pid") == "4d44"]
    busy = conns != "0" or dfu
    print(f"  dfu-remote connections {conns}, DFU gadgets {len(dfu)}")
    if busy and not force:
        sys.exit("ABORT: a flash looks to be in progress (--force to override)")
    if busy:
        print("  WARNING: proceeding over an apparently active flash (--force)")


def snapshot(pi):
    st = pi.api("GET", "/api/state")
    snap = {
        "relays": [(b["index"], r["num"], r["name"])
                   for b in st.get("boards", []) for r in b["relays"] if r["state"] == "on"],
        "gpios": [(g["wpi"], g["name"]) for g in st.get("gpios", []) if g["state"] == "on"],
        "pwm": [(c["index"], ch["ch"], ch["level"])
                for c in st.get("pwm", []) for ch in c["channels"] if ch["level"] > 0],
        # frequency is persisted config, so a restart keeps it; captured only
        # so the verify step can prove the restart did not disturb it
        "freq": [(c["index"], c["prescale"]) for c in st.get("pwm", [])],
    }
    print(f"  relays on : {[n for _, _, n in snap['relays']] or 'none'}")
    print(f"  gpios on  : {[n for _, n in snap['gpios']] or 'none'}")
    print(f"  pwm lit   : {snap['pwm'] or 'none'}")
    return snap


def restore(pi, snap):
    for b, r, name in snap["relays"]:
        pi.api("POST", "/api/relay/set", {"board": b, "relay": r, "state": True})
        print(f"  relay {b}/{r} {name}")
    for wpi, name in snap["gpios"]:
        pi.api("POST", "/api/gpio/set", {"wpi": wpi, "state": True})
        print(f"  gpio {wpi} {name}")
    for c, ch, lvl in snap["pwm"]:
        pi.api("POST", "/api/pwm/set", {"chip": c, "channel": ch, "level": lvl})
        print(f"  pwm {c}/ch{ch} -> {lvl}")


def verify(pi, snap):
    st = pi.api("GET", "/api/state")
    ok = True

    now = {(b["index"], r["num"]) for b in st.get("boards", []) for r in b["relays"] if r["state"] == "on"}
    want = {(b, r) for b, r, _ in snap["relays"]}
    if now != want:
        ok = False
        print(f"  RELAY MISMATCH missing={sorted(want - now)} extra={sorted(now - want)}")
    else:
        print(f"  relays match ({len(now)} on)")

    now = {(g["wpi"]) for g in st.get("gpios", []) if g["state"] == "on"}
    want = {w for w, _ in snap["gpios"]}
    if now != want:
        ok = False
        print(f"  GPIO MISMATCH missing={sorted(want - now)} extra={sorted(now - want)}")
    else:
        print(f"  gpios match ({len(now)} on)")

    now = {(c["index"], ch["ch"], ch["level"])
           for c in st.get("pwm", []) for ch in c["channels"] if ch["level"] > 0}
    if now != set(snap["pwm"]):
        ok = False
        print(f"  PWM MISMATCH want={sorted(snap['pwm'])} got={sorted(now)}")
    else:
        print(f"  pwm matches ({len(now)} lit)")

    now = [(c["index"], c["prescale"]) for c in st.get("pwm", [])]
    if now != snap["freq"]:
        ok = False
        print(f"  FREQUENCY DRIFTED want={snap['freq']} got={now}")

    for c in st.get("pwm", []):
        if c.get("error"):
            ok = False
            print(f"  PWM chip {c['index']} error: {c['error']}")
    for b in st.get("boards", []):
        if b.get("error"):
            ok = False
            print(f"  board {b['index']} error: {b['error']}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("RELAY_PANEL_SSH"),
                    help="ssh target for the Pi, e.g. root@bench-pi "
                         "(default: $RELAY_PANEL_SSH)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="deploy even if a flash appears to be in progress")
    args = ap.parse_args()
    if not args.host:
        ap.error("no target: pass --host or set RELAY_PANEL_SSH")
    pi = Pi(args.host)

    print("== changes ==")
    todo = changed_files(pi)
    if not todo:
        print("  nothing to deploy, remote already matches")
        return 0
    for rel, _, restart in todo:
        print(f"  {rel}{'  (needs restart)' if restart else ''}")
    if args.dry_run:
        return 0

    needs_restart = any(r for _, _, r in todo)

    print("== pre-flight ==")
    preflight(pi, args.force)

    snap = None
    if needs_restart:
        print("== snapshot ==")
        snap = snapshot(pi)

    print("== copy ==")
    for rel, remote, _ in todo:
        sh(f"scp -q {REPO / rel} {args.host}:{remote}")
        print(f"  {rel} -> {remote}")

    if not needs_restart:
        print("== done ==")
        print("  static only, no restart needed, bench untouched")
        return 0

    print("== restart ==")
    if any(rel == "relay-panel.service" for rel, _, _ in todo):
        pi.run("systemctl daemon-reload")
    pi.run(f"systemctl restart {SERVICE} && sleep 2")
    if pi.run(f"systemctl is-active {SERVICE}", check=False) != "active":
        print(pi.run(f"journalctl -u {SERVICE} -n 20 --no-pager -o cat", check=False))
        sys.exit("ABORT: service did not come back")
    print("  active")

    print("== restore ==")
    restore(pi, snap)

    print("== verify ==")
    return 0 if verify(pi, snap) else 1


if __name__ == "__main__":
    sys.exit(main())
