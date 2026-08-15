#!/usr/bin/env python3
"""Unit tests for the PCA9685 PWM logic in relay-panel.py.

Pure-function tests only - no I2C, no hardware, no server. Run with:

    python3 test-pwm.py
"""

import importlib.util
import sys

spec = importlib.util.spec_from_file_location("rp", "relay-panel.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

failures = []


def ck(name, got, want):
    if got == want:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: got {got!r} want {want!r}")


# --- duty encoding round-trip ---------------------------------------------
# Every level at every interesting phase must survive words->level unchanged,
# must stay inside a byte, and must never let a duty value set a FULL flag.
bad = []
for phase in (0, 1, 1024, 1365, 2048, 2731, 3072, 4095):
    for lvl in range(0, 4096):
        w = rp.pca_words(lvl, phase)
        if any(b < 0 or b > 0xFF for b in w):
            bad.append(("byte-range", lvl, phase, w))
        if 0 < lvl < rp.PWM_MAX and ((w[1] & rp.PCA_FULL) or (w[3] & rp.PCA_FULL)):
            bad.append(("full-bit-leak", lvl, phase, w))
        if rp.pca_level(w) != lvl:
            bad.append(("round-trip", lvl, phase, w, rp.pca_level(w)))
ck("4096 levels x 8 phases round-trip cleanly", bad[:3], [])

ck("level 0 -> FULL_OFF", rp.pca_words(0, 1024), [0x00, 0x00, 0x00, 0x10])
ck("level 4095 -> FULL_ON", rp.pca_words(4095, 1024), [0x00, 0x10, 0x00, 0x00])
ck("over-range clamps to FULL_ON", rp.pca_words(99999, 0), [0x00, 0x10, 0x00, 0x00])
ck("negative clamps to FULL_OFF", rp.pca_words(-5, 0), [0x00, 0x00, 0x00, 0x10])

# on=3072, off=(3072+2048) mod 4096 = 1024 -> high nibble 0x4
ck("phase wraps past the top of the cycle",
   rp.pca_words(2048, 3072), [0x00, 0x0C, 0x00, 0x04])

# datasheet 7.3.3: FULL_OFF takes precedence over FULL_ON
ck("FULL_OFF beats FULL_ON", rp.pca_level([0, 0x10, 0, 0x10]), 0)

# The state found on the bench chip before this code existed: a "100%" written
# as an unmasked 4096 landed on the FULL_ON bit with a stale OFF beside it.
ck("unmasked-4096 legacy state reads as full",
   rp.pca_level([0x00, 0x10, 0x00, 0x0C]), 4095)

# --- frequency <-> prescale ------------------------------------------------
ck("1526 Hz -> PRE_SCALE 3", rp.pca_prescale_for(1526), 3)
ck("24 Hz -> PRE_SCALE 253", rp.pca_prescale_for(24), 253)
ck("1000 Hz -> PRE_SCALE 5", rp.pca_prescale_for(1000), 5)
ck("above the ceiling clamps", rp.pca_prescale_for(50000), rp.PCA_PRESCALE_MIN)
ck("below the floor clamps", rp.pca_prescale_for(1), rp.PCA_PRESCALE_MAX)

ck("PRE_SCALE 3 = 1525.9 Hz", round(rp.pca_freq_for(3), 1), 1525.9)
ck("PRE_SCALE 253 = 24.0 Hz", round(rp.pca_freq_for(253), 1), 24.0)
ck("PRE_SCALE 255 = 23.8 Hz", round(rp.pca_freq_for(255), 1), 23.8)

# prescale_for must pick the *nearest* achievable frequency, not just floor it
for want in (24, 50, 100, 200, 400, 800, 1526):
    ps = rp.pca_prescale_for(want)
    here = abs(rp.pca_freq_for(ps) - want)
    for neighbour in (ps - 1, ps + 1):
        if rp.PCA_PRESCALE_MIN <= neighbour <= rp.PCA_PRESCALE_MAX:
            assert abs(rp.pca_freq_for(neighbour) - want) >= here - 1e-9, \
                f"{want} Hz: PRE_SCALE {neighbour} beats {ps}"
ck("prescale_for picks the nearest achievable frequency", True, True)

# --- channel config --------------------------------------------------------
ck("default phase ch0", rp.pca_channel_cfg({}, 0)[1], 0)
ck("default phase ch1", rp.pca_channel_cfg({}, 1)[1], 1024)
ck("default phase ch3", rp.pca_channel_cfg({}, 3)[1], 3072)
ck("default phase repeats every 4 channels", rp.pca_channel_cfg({}, 4)[1], 0)
ck("default name", rp.pca_channel_cfg({}, 7)[0], "Ch 7")
ck("bare-string channel entry tolerated",
   rp.pca_channel_cfg({"channels": {"0": "White"}}, 0), ("White", 0))
ck("explicit phase honoured",
   rp.pca_channel_cfg({"channels": {"2": {"name": "G", "phase": 1365}}}, 2),
   ("G", 1365))

# --- which channels a chip manages -----------------------------------------
ck("legacy size-only config expands to a contiguous range",
   rp.pca_enabled_channels({"size": 4}), [0, 1, 2, 3])
ck("channel map wins over size",
   rp.pca_enabled_channels({"size": 16, "channels": {"0": {}, "3": {}}}), [0, 3])
ck("channels need not be contiguous",
   rp.pca_enabled_channels({"channels": {"7": {}, "0": {}, "15": {}}}), [0, 7, 15])
ck("out-of-range channel keys are dropped",
   rp.pca_enabled_channels({"channels": {"0": {}, "16": {}, "-1": {}}}), [0])
ck("non-numeric channel keys are ignored",
   rp.pca_enabled_channels({"channels": {"0": {}, "eight": {}}}), [0])
ck("no size and no channels -> all 16",
   rp.pca_enabled_channels({}), list(range(16)))
ck("empty channel map falls back to size",
   rp.pca_enabled_channels({"size": 2, "channels": {}}), [0, 1])

legacy = {"size": 3}
mat = rp.pca_materialize_channels(legacy)
ck("materialize expands size into a map", sorted(mat), ["0", "1", "2"])
ck("materialize drops the now-vestigial size", "size" in legacy, False)
keep = {"channels": {"5": {"name": "IR"}}}
rp.pca_materialize_channels(keep)
ck("materialize leaves an existing map alone", keep["channels"], {"5": {"name": "IR"}})

# --- I2C address conflicts -------------------------------------------------
CFG = {
    "boards": [{"address": "0x20", "bus": 2, "name": "Bench Power"}],
    "pwm": [{"address": "0x40", "bus": 2, "name": "LED Strips"}],
}
ck("free address is free", rp.i2c_address_conflict(CFG, 2, "0x41"), None)
ck("same address on another bus is free", rp.i2c_address_conflict(CFG, 3, "0x40"), None)
ck("relay board address is refused",
   "Bench Power" in (rp.i2c_address_conflict(CFG, 2, "0x20") or ""), True)
ck("another PWM chip's address is refused",
   "LED Strips" in (rp.i2c_address_conflict(CFG, 2, "0x40") or ""), True)
ck("a chip does not conflict with itself",
   rp.i2c_address_conflict(CFG, 2, "0x40", skip_pwm=0), None)

# --- channel groups --------------------------------------------------------
RGB = {"size": 4, "groups": [{"name": "RGB Strip", "channels": [1, 2, 3]}]}

ck("group defaults", rp.pca_group_cfg(RGB, 0),
   ("RGB Strip", [1, 2, 3], {1: 4095, 2: 4095, 3: 4095}))
ck("unnamed group gets an index name",
   rp.pca_group_cfg({"size": 4, "groups": [{"channels": [0]}]}, 0)[0], "Group 0")
ck("channels past the chip size are dropped",
   rp.pca_group_cfg({"size": 4, "groups": [{"channels": [1, 2, 9, -1]}]}, 0)[1], [1, 2])
ck("duplicate channels are deduped",
   rp.pca_group_cfg({"size": 4, "groups": [{"channels": [1, 2, 1]}]}, 0)[1], [1, 2])
ck("out-of-range group index is inert", rp.pca_group_cfg(RGB, 7)[1], [])
# a group can only cover channels the chip still manages, so removing a
# channel cannot leave a group pointing at one that is gone
ck("group members outside the managed set are dropped",
   rp.pca_group_cfg({"channels": {"1": {}, "3": {}},
                     "groups": [{"channels": [1, 2, 3]}]}, 0)[1], [1, 3])

_, chans, wts = rp.pca_group_cfg(RGB, 0)

# equal weights: a group is plainly "set every member the same"
ck("equal weights set members equal",
   rp.group_levels_for(chans, wts, 2048), {1: 2048, 2: 2048, 3: 2048})
ck("group master 0 blanks members",
   rp.group_levels_for(chans, wts, 0), {1: 0, 2: 0, 3: 0})
ck("group master clamps", rp.group_levels_for(chans, wts, 99999),
   {1: 4095, 2: 4095, 3: 4095})

# unequal weights hold a calibrated white point while the master dims
CAL = {"size": 4, "groups": [{"name": "RGB", "channels": [1, 2, 3],
                              "weights": {"1": 4095, "2": 2048, "3": 1024}}]}
_, cchans, cwts = rp.pca_group_cfg(CAL, 0)
ck("weighted group at full", rp.group_levels_for(cchans, cwts, 4095),
   {1: 4095, 2: 2048, 3: 1024})
ck("weighted group holds ratio at half", rp.group_levels_for(cchans, cwts, 2048),
   {1: 2048, 2: 1024, 3: 512})

# master is derived from hardware, never stored
ck("master recovered from equal members",
   rp.group_master_from(chans, wts, {1: 2048, 2: 2048, 3: 2048}), 2048)
ck("master recovered from weighted members",
   rp.group_master_from(cchans, cwts, {1: 2048, 2: 1024, 3: 512}), 2048)
ck("all members dark -> master 0",
   rp.group_master_from(chans, wts, {1: 0, 2: 0, 3: 0}), 0)
# Reference is the heaviest member (ties -> first), not the brightest: a lone
# channel raised by hand shows up as `mixed`, it does not drag the master.
ck("master is referenced to the heaviest member",
   rp.group_master_from(chans, wts, {1: 100, 2: 3000, 3: 0}), 100)
ck("weighted group references its full-weight member",
   rp.group_master_from(cchans, cwts, {1: 2048, 2: 4095, 3: 4095}), 2048)
ck("zero-weight member cannot drive the master",
   rp.group_master_from([1], {1: 0}, {1: 4095}), 0)

bad = []
for m in range(0, 4096, 7):
    for cs, ws in ((chans, wts), (cchans, cwts)):
        back = rp.group_master_from(cs, ws, rp.group_levels_for(cs, ws, m))
        if abs(back - m) > 1:                     # 1 count of rounding slack
            bad.append((m, back))
ck("master -> levels -> master round-trips", bad[:3], [])

# a member nudged off its ratio must surface as mixed, not silently skew the master
g = rp.get_pwm_groups(RGB, {1: 2048, 2: 2048, 3: 2048})[0]
ck("even group is not mixed", (g["level"], g["mixed"]), (2048, False))
g = rp.get_pwm_groups(RGB, {1: 2048, 2: 2048, 3: 100})[0]
ck("uneven group reports mixed", (g["level"], g["mixed"]), (2048, True))
g = rp.get_pwm_groups(CAL, {1: 4095, 2: 2048, 3: 1024})[0]
ck("calibrated ratio is not mixed", (g["level"], g["mixed"]), (4095, False))
ck("no groups configured -> empty list", rp.get_pwm_groups({"size": 4}, {}), [])

# --- request body parsing --------------------------------------------------
ck("level passthrough", rp.resolve_level({"level": 2048}), 2048)
ck("percent 50", rp.resolve_level({"percent": 50}), 2048)
ck("percent 100", rp.resolve_level({"percent": 100}), 4095)
ck("percent 0", rp.resolve_level({"percent": 0}), 0)
ck("level 0 is not treated as absent", rp.resolve_level({"level": 0}), 0)
ck("neither key -> None", rp.resolve_level({}), None)
ck("level over-range clamps", rp.resolve_level({"level": 99999}), 4095)

# --- i2cdump parsing -------------------------------------------------------
# Verbatim shape of `i2cdump -y 2 0x40 b` output, including the ASCII column.
DUMP = """     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f    0123456789abcdef
00: 00 04 e2 e4 e8 e0 00 10 00 0c 00 00 00 10 00 00    .?????.?.?...?..
10: 00 10 00 00 00 10 00 00 00 10 00 00 00 10 00 00    .?...?...?...?..
f0: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 03 00    ..............?."""

saved = rp.i2c_cmd
rp.i2c_cmd = lambda *a, **k: (DUMP, 0)
regs = rp.pca_dump(2, "0x40")
ck("parses MODE2", regs[0x01], 0x04)
ck("parses PRE_SCALE at 0xFE", regs[0xFE], 0x03)
ck("parses ch0 registers", regs[0x06:0x0A], [0x00, 0x10, 0x00, 0x0C])
ck("ch1 decodes as off", rp.pca_level(regs[0x0A:0x0E]), 0)
ck("ch2 decodes as off", rp.pca_level(regs[0x0E:0x12]), 0)

rp.i2c_cmd = lambda *a, **k: ("", 1)
ck("failed dump returns None", rp.pca_dump(2, "0x40"), None)

# An absent chip: i2cdump exits 0 and prints a full page of XX, so the parse
# coming back empty is the only signal that nothing answered.
rp.i2c_cmd = lambda *a, **k: ("00: XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX", 0)
ck("an all-XX dump reads as absent", rp.pca_dump(2, "0x41"), None)

rp.i2c_cmd = lambda *a, **k: ("00: 20 XX 04 XX XX XX XX XX XX XX XX XX XX XX XX XX", 0)
part = rp.pca_dump(2, "0x40")
ck("a partly-unreadable chip still parses", (part[0x00], part[0x02]), (0x20, 0x04))
rp.i2c_cmd = saved

# --- relay reads must not fake "all off" on a dead bus ----------------------
saved = rp.i2c_cmd
rp.i2c_cmd = lambda *a, **k: ("0x81", 0)
ck("a good read returns both ports", rp.i2c_read_state(2, "0x20"), (0x81, 0x81))

rp.i2c_cmd = lambda *a, **k: ("Error: Read failed", 2)
try:
    rp.i2c_read_state(2, "0x20")
    ck("a dead bus raises instead of reporting all-off", "returned", "raised")
except RuntimeError as e:
    ck("a dead bus raises instead of reporting all-off", "no response" in str(e), True)

# a truncated/garbled reply must not be parsed as a real port value either
rp.i2c_cmd = lambda *a, **k: ("", 0)
try:
    rp.i2c_read_state(2, "0x20")
    ck("an empty reply raises", "returned", "raised")
except RuntimeError:
    ck("an empty reply raises", True, True)
rp.i2c_cmd = saved

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all pass")
