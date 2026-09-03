#!/usr/bin/env python3
"""Bus priority, snapshot caching and init detection, without touching hardware.

The rest of the suite needs a bench; this part is pure concurrency, so it runs
anywhere and is the cheap place to catch a lock that lets a click starve.

Usage: ./test-concurrency.py
"""
import importlib.util, os, pathlib, sys, threading, time

os.environ["RELAY_PANEL_DIR"] = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "rp", str(pathlib.Path(__file__).resolve().parent / "relay-panel.py"))
rp = importlib.util.module_from_spec(spec); spec.loader.exec_module(rp)

fails = []
def ck(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok: fails.append(name)

def ck_true(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond: fails.append(name)

print("--- BusLock: mutual exclusion ---")
bus = rp.BusLock()
inside, overlap = 0, []
def worker():
    global inside
    for _ in range(50):
        with bus:
            inside += 1
            if inside > 1: overlap.append(inside)
            time.sleep(0.0005)
            inside -= 1
ts = [threading.Thread(target=worker) for _ in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
ck("no two foreground holders overlap", overlap, [])

print("\n--- BusLock: a click does not queue behind background work ---")
bus = rp.BusLock()
order = []
stop = threading.Event()
def sampler():
    # Mimics the 1 Hz sweep: repeated background holds, each ~40 ms.
    while not stop.is_set():
        with bus.background():
            order.append("bg")
            time.sleep(0.04)
def click():
    time.sleep(0.12)                      # let several sweeps get going
    t0 = time.monotonic()
    with bus:
        order.append("click")
    return time.monotonic() - t0

bgs = [threading.Thread(target=sampler, daemon=True) for _ in range(4)]
[t.start() for t in bgs]
waited = click()
stop.set(); time.sleep(0.1)
ck_true("click waits under one background hold", waited < 0.06, f"({waited*1000:.0f} ms)")
ck_true("background work was actually running", order.count("bg") > 3,
        f"({order.count('bg')} sweeps)")

print("\n--- BusLock: background is not starved forever ---")
bus = rp.BusLock()
got = threading.Event()
def hog():
    for _ in range(40):
        with bus:
            time.sleep(0.01)
def bg():
    with bus.background(patience=0.15):
        got.set()
threading.Thread(target=hog, daemon=True).start()
time.sleep(0.02)
threading.Thread(target=bg, daemon=True).start()
ck_true("background gets in despite a write storm", got.wait(1.5))

print("\n--- BenchSampler: cache, staleness, nudge ---")
class FakeSampler(rp.BenchSampler):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.reads = 0
    def sample(self, background=True):
        self.reads += 1
        return {"boards": [], "gpios": [], "pwm": [], "usb": [],
                "usb_vid": "a108", "n": self.reads}

s = FakeSampler(interval=0.05)
ck("no snapshot before the first read", s.latest(), (None, None, 0))
p = s.payload()
ck("payload falls back to a fresh read", p["n"], 1)
ck("a fresh payload is not stale", p["stale"], False)
ck("no hardware read for the second caller", (s.payload()["n"], s.reads), (1, 1))

s._at = time.monotonic() - (rp.STALE_AFTER + 1)
ck("an old snapshot is flagged stale", s.payload()["stale"], True)
ck_true("stale payload reports its age",
        s.payload()["sampled_age_s"] > rp.STALE_AFTER)
ck("fresh=1 bypasses the snapshot", s.payload(fresh=True)["n"], 2)

print("\n--- BenchSampler: nudge resamples early ---")
s = FakeSampler(interval=10.0)
s.start()
time.sleep(0.15)
before = s.reads
s.nudge()
time.sleep(0.15)
ck_true("nudge took an extra sample", s.reads == before + 1,
        f"({before} -> {s.reads}, interval 10 s)")
time.sleep(0.2)
ck_true("no runaway resampling after one nudge", s.reads == before + 1,
        f"({s.reads})")

print("\n--- timeouts split by priority ---")
ck("background reads fail fast", rp.bus_timeout(True), rp.BUS_BACKGROUND_TIMEOUT)
ck("writes stay patient", rp.bus_timeout(False), rp.BUS_TIMEOUT)
ck_true("background timeout is well under the sample interval",
        rp.BUS_BACKGROUND_TIMEOUT < rp.SAMPLE_INTERVAL,
        f"({rp.BUS_BACKGROUND_TIMEOUT}s < {rp.SAMPLE_INTERVAL}s)")

print("\n--- pca_extclk_stuck: a re-init cannot fix a sticky bit ---")
def m1(v):
    r = [0] * 256
    r[rp.PCA_MODE1] = v
    return r
ck("normal chip is not flagged", rp.pca_extclk_stuck(m1(0x20)), False)
ck("EXTCLK set is flagged", rp.pca_extclk_stuck(m1(0x60)), True)
ck("no readings, nothing to say", rp.pca_extclk_stuck(None), False)
# It must not also come back as "needs init", or the poll would re-run init
# every second against a bit that writing cannot clear.
ck_true("EXTCLK is reported separately from needs_init",
        rp.pca_extclk_stuck(m1(0x60)) and not rp.pca_needs_init(m1(0x60), 0))

print("\n--- MODE1 written at init answers on its own address only ---")
ck_true("oscillator settle is at least the datasheet's 500 us",
        rp.PCA_OSC_SETTLE >= 0.0005, f"({rp.PCA_OSC_SETTLE * 1e6:.0f} us)")
ck("SUB1/2/3 and ALLCALL are the extra addresses", rp.PCA_M1_EXTRA_ADDRS, 0x0F)

print("\n--- write barrier: a snapshot must say what it covers ---")
start = rp.current_write_seq()
ck("bump advances the sequence", rp.bump_write_seq(), start + 1)
ck("and is readable", rp.current_write_seq(), start + 1)

class SeqSampler(rp.BenchSampler):
    def sample(self, background=True):
        return {"boards": [], "gpios": [], "pwm": [], "usb": [], "usb_vid": "a108"}

q = SeqSampler(interval=10.0)
q.fresh()
p0 = q.payload()
ck("payload reports what it covers", p0["covers_writes"], rp.current_write_seq())

# A write answered after the sweep must leave the snapshot visibly behind, or
# the client repaints from a picture taken before its own click.
seq_after = rp.bump_write_seq()
ck_true("an older snapshot is behind the new write",
        q.payload()["covers_writes"] < seq_after,
        f"({q.payload()['covers_writes']} < {seq_after})")
q.fresh()
ck_true("a re-read catches up",
        q.payload()["covers_writes"] >= seq_after,
        f"({q.payload()['covers_writes']} >= {seq_after})")

print("\n--- pca_needs_init: telling a fresh chip from a set-up one ---")
M1, PS = rp.PCA_MODE1, rp.PCA_PRESCALE
def regs_with(mode1, prescale):
    r = [0] * 256
    r[M1], r[PS] = mode1, prescale
    return r

# 0x11 / 0x1e are the PCA9685's power-on defaults, i.e. what the panel sees
# after its own init_boards has cut the rail the chip lives on.
ck("a just-powered chip needs init", rp.pca_needs_init(regs_with(0x11, 0x1e), 3), True)
ck("a set-up chip is left alone", rp.pca_needs_init(regs_with(0x20, 0x03), 3), False)
ck("allcall set is still set up", rp.pca_needs_init(regs_with(0x21, 0x03), 3), False)
ck("auto-increment clear needs init", rp.pca_needs_init(regs_with(0x01, 0x03), 3), True)
ck("still asleep needs init", rp.pca_needs_init(regs_with(0x30, 0x03), 3), True)
ck("wrong prescale needs init", rp.pca_needs_init(regs_with(0x20, 0x1e), 3), True)

print("\n--- pin_mismatch: commanded outputs vs actual pins ---")
mm = rp.pin_mismatch
ck("healthy 16-way board is quiet", mm(16, 0x80, 0x80, 0x80, 0x80, ), None)
ck("all-off healthy board is quiet", mm(16, 0x00, 0x00, 0x00, 0x00), None)
ck_true("port A disagreement is caught", mm(16, 0x01, 0x00, 0x00, 0x00) is not None)
ck_true("port B disagreement is caught", mm(16, 0x00, 0x80, 0x00, 0x00) is not None)
# The real failure: direction registers never configured, so every write is
# accepted and echoed while the pins stay put. Measured on the bench.
ck_true("an unconfigured board is caught",
        mm(16, 0xff, 0xff, 0x00, 0x00) is not None)
ck("an 8-way board ignores port B", mm(8, 0x0f, 0x00, 0x0f, 0xff), None)
ck_true("the warning names both sides",
        "0xff" in mm(16, 0xff, 0xff, 0x00, 0x00)
        and "0x00" in mm(16, 0xff, 0xff, 0x00, 0x00))

print("\n--- parse_state: \"off\" must never mean on ---")
for v in (True, "on", "ON", " true ", "1", 1, "yes"):
    ck(f"{v!r} is on", rp.parse_state(v), True)
for v in (False, "off", "OFF", " false ", "0", 0, "no"):
    ck(f"{v!r} is off", rp.parse_state(v), False)
for v in ("", "maybe", None, 2, [], {"a": 1}):
    try:
        rp.parse_state(v)
        ck_true(f"{v!r} is rejected", False, "(was accepted)")
    except ValueError:
        ck_true(f"{v!r} is rejected", True)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
