#!/usr/bin/env python3
"""Hardware tests for relay-panel's PWM support. Drives the real PCA9685.

Unlike test-pwm.py (pure logic, runs anywhere), this talks to a live panel and
lights real strips. It snapshots the whole PWM configuration first and restores
it in a finally block, so an assertion failure mid-run cannot leave the bench in
a half-configured state.

Run it on the Orange Pi so the register-level checks can read the chip
directly; run it anywhere that can reach the panel and those checks skip.

    ssh root@your-pi python3 - < test-hardware.py
    ./test-hardware.py --url http://your-pi:8080

It never touches relays, GPIOs, or channels outside the chip under test, and
uses channels from --spare for add/remove work so the live rig is left alone.
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

FULL = 0x10          # FULL_ON / FULL_OFF bit in the high registers
PWM_MAX = 4095
LED0_BASE = 0x06

failures = []
skipped = 0
BASE = "http://localhost:8080"
CHIP = 0
SPARE = []
HAVE_I2C = False


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def chip():
    return api("GET", "/api/pwm")[1]["pwm"][CHIP]


def levels():
    return {c["ch"]: c["level"] for c in chip()["channels"]}


def ck(name, got, want):
    if got == want:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: got {got!r} want {want!r}")


def skip(name, why):
    global skipped
    skipped += 1
    print(f"skip {name} ({why})")


def regs(ch):
    """Read one channel's four registers straight off the chip."""
    c = chip()
    out = []
    for off in range(4):
        r = subprocess.run(
            ["i2cget", "-y", str(c["bus"]), c["address"], hex(LED0_BASE + 4 * ch + off)],
            capture_output=True, text=True)
        out.append(int(r.stdout.strip(), 16))
    return out


# --- snapshot / restore ----------------------------------------------------

def snapshot():
    c = chip()
    return {
        "name": c["name"],
        "freq": c["freq_hz"],
        "prescale": c["prescale"],
        "channels": [{"ch": x["ch"], "name": x["name"], "phase": x["phase"],
                      "level": x["level"]} for x in c["channels"]],
        "groups": [{"name": g["name"], "channels": list(g["channels"]),
                    "weights": dict(g["weights"]), "level": g["level"]}
                   for g in c["groups"]],
    }


def restore(snap):
    """Put the chip back exactly as found, whatever the tests did to it."""
    want = {x["ch"] for x in snap["channels"]}
    have = {x["ch"] for x in chip()["channels"]}
    for ch in sorted(have - want):
        api("DELETE", f"/api/pwm/channels/{CHIP}/{ch}")
    for x in snap["channels"]:
        if x["ch"] not in have:
            api("POST", "/api/pwm/channels",
                {"chip": CHIP, "channel": x["ch"], "name": x["name"]})
        api("PUT", "/api/pwm/name",
            {"chip": CHIP, "channel": x["ch"], "name": x["name"]})
        api("PUT", "/api/pwm/phase",
            {"chip": CHIP, "channel": x["ch"], "phase": x["phase"]})

    # groups are positional, so rebuild rather than try to match them up
    for gi in range(len(chip()["groups"]) - 1, -1, -1):
        api("DELETE", f"/api/pwm/groups/{CHIP}/{gi}")
    for g in snap["groups"]:
        _, r = api("POST", "/api/pwm/groups",
                   {"chip": CHIP, "name": g["name"], "channels": g["channels"]})
        if "group" in r:
            api("PUT", "/api/pwm/group",
                {"chip": CHIP, "group": r["group"], "weights": g["weights"]})

    api("PUT", "/api/pwm/name", {"chip": CHIP, "name": snap["name"]})
    api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": snap["freq"]})
    api("POST", "/api/pwm/off", {"chip": CHIP})
    for x in snap["channels"]:
        if x["level"]:
            api("POST", "/api/pwm/set",
                {"chip": CHIP, "channel": x["ch"], "level": x["level"]})


def check_restored(snap):
    c = chip()
    ck("restored: chip name", c["name"], snap["name"])
    ck("restored: frequency", c["prescale"], snap["prescale"])
    ck("restored: channel set", [x["ch"] for x in c["channels"]],
       [x["ch"] for x in snap["channels"]])
    ck("restored: channel names", [x["name"] for x in c["channels"]],
       [x["name"] for x in snap["channels"]])
    ck("restored: phases", [x["phase"] for x in c["channels"]],
       [x["phase"] for x in snap["channels"]])
    ck("restored: levels", [x["level"] for x in c["channels"]],
       [x["level"] for x in snap["channels"]])
    ck("restored: groups", [(g["name"], g["channels"]) for g in c["groups"]],
       [(g["name"], g["channels"]) for g in snap["groups"]])


# --- tests -----------------------------------------------------------------

def test_duty_encoding(snap):
    print("\n--- duty encoding on real hardware ---")
    ch = snap["channels"][0]["ch"]
    api("PUT", "/api/pwm/phase", {"chip": CHIP, "channel": ch, "phase": 0})
    if not HAVE_I2C:
        skip("register-level duty encoding", "i2cget not on this host")
    else:
        api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": 0})
        ck("level 0 -> FULL_OFF", regs(ch), [0x00, 0x00, 0x00, FULL])
        api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": 1})
        ck("level 1 -> on=0 off=1", regs(ch), [0x00, 0x00, 0x01, 0x00])
        api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": 2048})
        ck("level 2048 -> off=0x800", regs(ch), [0x00, 0x00, 0x00, 0x08])
        api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": PWM_MAX})
        ck("level 4095 -> FULL_ON", regs(ch), [0x00, FULL, 0x00, 0x00])

        # A duty that wraps past the top of the cycle must land on 0x08 in the
        # high byte, not 0x10 - unmasked, it would read back as FULL_ON.
        api("PUT", "/api/pwm/phase", {"chip": CHIP, "channel": ch, "phase": 2048})
        api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": 2048})
        ck("wrapped duty stays clear of the FULL bit", regs(ch), [0x00, 0x08, 0x00, 0x00])
        ck("wrapped duty reads back correctly", levels()[ch], 2048)
        api("PUT", "/api/pwm/phase", {"chip": CHIP, "channel": ch, "phase": 0})

    print("\n--- percent and read-back ---")
    _, r = api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "percent": 25})
    ck("percent 25 -> 1024 counts", r["level"], 1024)
    bad = []
    for lvl in (0, 1, 7, 255, 256, 1000, 2047, 2048, 4094, 4095):
        api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": lvl})
        if levels()[ch] != lvl:
            bad.append((lvl, levels()[ch]))
    ck("10 levels survive write -> hardware -> read", bad, [])


def test_all_and_off(snap):
    print("\n--- all on / all off ---")
    n = len(snap["channels"])
    api("POST", "/api/pwm/all", {"chip": CHIP, "level": PWM_MAX})
    ck("all-on sets every managed channel",
       [c["level"] for c in chip()["channels"]], [PWM_MAX] * n)
    api("POST", "/api/pwm/off", {"chip": CHIP})
    ck("all-off blanks every managed channel",
       [c["level"] for c in chip()["channels"]], [0] * n)
    if HAVE_I2C:
        c = chip()
        managed = {x["ch"] for x in c["channels"]}
        unmanaged = [x for x in range(16) if x not in managed]
        ck("all-off reaches unmanaged channels too",
           all(regs(x)[3] & FULL for x in unmanaged[:4]), True)
    else:
        skip("all-off reaches unmanaged channels", "i2cget not on this host")


def test_frequency(snap):
    print("\n--- frequency ---")
    for want, ps in ((1526, 3), (800, 7), (400, 14), (200, 30),
                     (100, 60), (50, 121), (24, 253)):
        _, r = api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": want})
        ck(f"{want:>4} Hz -> PRE_SCALE {ps} ({r['freq_hz']} Hz actual)",
           (r["prescale"], chip()["prescale"]), (ps, ps))

    ch = snap["channels"][0]["ch"]
    api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": 1526})
    api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": 1234})
    api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": 400})
    ck("duty survives the sleep/restart cycle a frequency change needs",
       levels()[ch], 1234)
    api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": 1526})

    if HAVE_I2C:
        c = chip()
        mode1 = int(subprocess.run(["i2cget", "-y", str(c["bus"]), c["address"], "0x00"],
                                   capture_output=True, text=True).stdout.strip(), 16)
        ck("auto-increment left enabled", bool(mode1 & 0x20), True)
        ck("chip left awake", bool(mode1 & 0x10), False)
    else:
        skip("MODE1 register checks", "i2cget not on this host")


def test_groups(snap):
    print("\n--- channel groups ---")
    if len(snap["channels"]) < 2:
        skip("group tests", "chip manages fewer than 2 channels")
        return
    members = [x["ch"] for x in snap["channels"][:3]]
    outsider = [x["ch"] for x in snap["channels"] if x["ch"] not in members]

    _, r = api("POST", "/api/pwm/groups",
               {"chip": CHIP, "name": "HW Test", "channels": members})
    gi = r["group"]
    ck("weights default to full", chip()["groups"][gi]["weights"],
       {str(c): PWM_MAX for c in members})

    api("POST", "/api/pwm/off", {"chip": CHIP})
    api("POST", "/api/pwm/group", {"chip": CHIP, "group": gi, "level": 2048})
    lv = levels()
    ck("master drives every member", [lv[c] for c in members], [2048] * len(members))
    if outsider:
        ck("master leaves non-members alone", [lv[c] for c in outsider],
           [0] * len(outsider))

    g = chip()["groups"][gi]
    ck("master reads back", g["level"], 2048)
    ck("an even group is not mixed", g["mixed"], False)

    api("POST", "/api/pwm/set", {"chip": CHIP, "channel": members[-1], "level": 77})
    ck("a member nudged alone reports mixed", chip()["groups"][gi]["mixed"], True)
    api("POST", "/api/pwm/group", {"chip": CHIP, "group": gi, "level": 1024})
    ck("re-driving the master clears mixed", chip()["groups"][gi]["mixed"], False)

    if len(members) >= 3:
        w = {str(members[0]): 4095, str(members[1]): 3200, str(members[2]): 2600}
        api("PUT", "/api/pwm/group", {"chip": CHIP, "group": gi, "weights": w})
        api("POST", "/api/pwm/group", {"chip": CHIP, "group": gi, "level": PWM_MAX})
        lv = levels()
        ck("at full master, members sit at their weights",
           [lv[c] for c in members], [4095, 3200, 2600])
        api("POST", "/api/pwm/group", {"chip": CHIP, "group": gi, "level": 2048})
        lv = levels()
        ck("at half master, the ratio holds", [lv[c] for c in members], [2048, 1600, 1300])
        ck("a calibrated ratio is not mixed", chip()["groups"][gi]["mixed"], False)

    _, r = api("PUT", "/api/pwm/group",
               {"chip": CHIP, "group": gi, "name": "Renamed", "channels": members[:2]})
    ck("group renamed and re-scoped", (r["name"], r["channels"]), ("Renamed", members[:2]))
    code, _ = api("PUT", "/api/pwm/group", {"chip": CHIP, "group": gi, "channels": [99]})
    ck("re-scoping onto an unmanaged channel is refused", code, 400)

    api("DELETE", f"/api/pwm/groups/{CHIP}/{gi}")
    ck("group deleted", len(chip()["groups"]), len(snap["groups"]))


def test_crud(snap):
    print("\n--- add / remove channels ---")
    if len(SPARE) < 2:
        skip("channel add/remove", "need 2 spare channels, pass --spare")
        return
    a, b = SPARE[0], SPARE[1]

    api("POST", "/api/pwm/channels", {"chip": CHIP, "channel": a, "name": "HW Test A"})
    api("POST", "/api/pwm/channels", {"chip": CHIP, "channel": b})
    c = chip()
    got = {x["ch"]: x for x in c["channels"]}
    ck("added channels appear", (a in got, b in got), (True, True))
    ck("named on add", got[a]["name"], "HW Test A")
    ck("unnamed gets a default", got[b]["name"], f"Ch {b}")
    ck("added channels start dark", (got[a]["level"], got[b]["level"]), (0, 0))
    ck("available shrinks", (a in c["available"], b in c["available"]), (False, False))

    code, _ = api("POST", "/api/pwm/channels", {"chip": CHIP, "channel": a})
    ck("re-adding a managed channel is refused", code, 400)
    code, _ = api("POST", "/api/pwm/channels", {"chip": CHIP, "channel": 16})
    ck("channel 16 does not exist", code, 400)

    print("\n--- groups follow channel removal ---")
    _, r = api("POST", "/api/pwm/groups",
               {"chip": CHIP, "name": "Throwaway", "channels": [a, b]})
    gi = r["group"]
    api("POST", "/api/pwm/group", {"chip": CHIP, "group": gi, "level": 2048})
    ck("throwaway group drives both", [levels()[x] for x in (a, b)], [2048, 2048])

    _, r = api("DELETE", f"/api/pwm/channels/{CHIP}/{a}")
    ck("a group with a survivor is kept", r.get("groups_dropped"), [])
    ck("group pruned to the survivor",
       [g for g in chip()["groups"] if g["name"] == "Throwaway"][0]["channels"], [b])
    if HAVE_I2C:
        ck("the removed channel was blanked, not left lit", regs(a)[3] & FULL, FULL)

    _, r = api("DELETE", f"/api/pwm/channels/{CHIP}/{b}")
    ck("removing the last member drops the group", r.get("groups_dropped"), ["Throwaway"])
    code, _ = api("DELETE", f"/api/pwm/channels/{CHIP}/{a}")
    ck("removing an unmanaged channel is refused", code, 400)
    code, _ = api("POST", "/api/pwm/set", {"chip": CHIP, "channel": a, "level": 100})
    ck("an unmanaged channel is not addressable", code, 400)


def test_address_guard(snap):
    print("\n--- address conflict guard ---")
    _, st = api("GET", "/api/state?fresh=1")
    boards = st.get("boards", [])
    if not boards:
        skip("relay-board conflict guard", "no relay boards configured")
    else:
        taken = boards[0]
        code, r = api("PUT", "/api/pwm/settings",
                      {"chip": CHIP, "address": taken["address"], "bus": taken["bus"]})
        ck("moving onto a relay board is refused", code, 400)
        ck("...and names the board", taken["name"] in r.get("error", ""), True)
        ck("...and the chip did not move", chip()["address"], snap.get("_address"))

    c = chip()
    code, _ = api("POST", "/api/pwm", {"name": "dupe", "address": c["address"], "bus": c["bus"]})
    ck("adding a chip at a taken address is refused", code, 400)
    ck("no stray chips were created", len(api("GET", "/api/pwm")[1]["pwm"]),
       snap["_chipcount"])


def test_reset(snap):
    print("\n--- reset ---")
    ch = snap["channels"][0]["ch"]
    api("POST", "/api/pwm/set", {"chip": CHIP, "channel": ch, "level": 3000})
    # frequency is persisted config, so reset must re-apply what is configured
    # now rather than any hardcoded default
    api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": 200})
    code, r = api("POST", "/api/pwm/reset", {"chip": CHIP})
    ck("reset accepted", (code, r.get("ok")), (200, True))
    c = chip()
    ck("reset blanks every channel", [x["level"] for x in c["channels"]],
       [0] * len(c["channels"]))
    ck("reset re-applies the configured frequency, not a default", c["prescale"], 30)
    api("PUT", "/api/pwm/freq", {"chip": CHIP, "freq_hz": snap["freq"]})


def main():
    global BASE, CHIP, SPARE, HAVE_I2C
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=BASE, help="panel base URL")
    ap.add_argument("--chip", type=int, default=0, help="PWM chip index to exercise")
    ap.add_argument("--spare", default="7,8",
                    help="comma-separated unwired channels safe to add/remove")
    args = ap.parse_args()
    BASE = args.url.rstrip("/")
    CHIP = args.chip
    SPARE = [int(x) for x in args.spare.split(",") if x.strip()]
    HAVE_I2C = shutil.which("i2cget") is not None

    code, r = api("GET", "/api/pwm")
    if code != 200 or CHIP >= len(r.get("pwm", [])):
        sys.exit(f"no PWM chip {CHIP} at {BASE}")
    if r["pwm"][CHIP]["error"]:
        sys.exit(f"chip {CHIP} is not responding: {r['pwm'][CHIP]['error']}")

    snap = snapshot()
    snap["_address"] = r["pwm"][CHIP]["address"]
    snap["_chipcount"] = len(r["pwm"])
    print(f"chip {CHIP} '{snap['name']}' at {snap['_address']}, "
          f"channels {[x['ch'] for x in snap['channels']]}, "
          f"groups {[g['name'] for g in snap['groups']]}, {snap['freq']} Hz")
    print(f"register-level checks: {'on' if HAVE_I2C else 'SKIPPED (no i2cget)'}")

    try:
        test_duty_encoding(snap)
        test_all_and_off(snap)
        test_frequency(snap)
        test_groups(snap)
        test_crud(snap)
        test_address_guard(snap)
        test_reset(snap)
    finally:
        # runs even on an assertion blow-up, so a failed run cannot leave the
        # bench half-configured the way an early-exit script would
        print("\n--- restore ---")
        restore(snap)
        check_restored(snap)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print(f"all pass ({skipped} skipped)" if skipped else "all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
