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

rp.i2c_cmd = lambda *a, **k: ("00: XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX", 0)
ck("unreadable registers do not crash the parser", rp.pca_dump(2, "0x40")[0], 0)
rp.i2c_cmd = saved

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all pass")
