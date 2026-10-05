#!/usr/bin/env python3
"""Relay Control Panel - Web interface for XL9535 I2C relay boards."""

import hashlib
import ipaddress
import json
import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from uart_stream import UartHub, UartNotConnected

BASE_DIR = Path(os.environ.get("RELAY_PANEL_DIR", "/opt/relay-panel"))
CONFIG_FILE = BASE_DIR / "config.json"
TOKENS_FILE = BASE_DIR / "tokens.json"
STATIC_DIR = BASE_DIR / "static"
LISTEN_HOST = os.environ.get("RELAY_PANEL_HOST", "::")
LISTEN_PORT = int(os.environ.get("RELAY_PANEL_PORT", "8080"))
# Write and sample counters belong to one process lifetime. Browsers use this
# identifier to discard late replies and reset their counters after a restart.
SERVER_ID = uuid.uuid4().hex

# Same raw TCP consoles as uart-monitor: ttyUSB0..23 at ports 3000..3023.
# These addresses are operator configuration, never supplied by an API caller.
UART_HOST = os.environ.get("RELAY_PANEL_UART_HOST", "127.0.0.1")
UART_BASE_PORT = int(os.environ.get("RELAY_PANEL_UART_BASE_PORT", "3000"))
UART_COUNT = int(os.environ.get("RELAY_PANEL_UART_COUNT", "24"))
uart_hub = UartHub(UART_HOST, UART_BASE_PORT, UART_COUNT)
UART_HEARTBEAT = 5.0
UART_INPUT_MAX = 4096      # bytes per request; the browser splits longer pastes

# Callers allowed to act without a token, i.e. with full control of the bench.
# Defaults to loopback and private addressing, on the assumption that the panel
# is only routable from a network you own. Expose it any further - a VPN for an
# outside collaborator, say - and set RELAY_PANEL_TRUSTED to just the ranges
# that should keep unrestricted access; everyone else then needs a token.
TRUSTED_NETS = [
    ipaddress.ip_network(n.strip())
    for n in os.environ.get(
        "RELAY_PANEL_TRUSTED",
        "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7",
    ).split(",")
    if n.strip()
]

USB_SYS_PATH = Path("/sys/bus/usb/devices")
# Which USB vendor to watch for. Defaults to Ingenic, whose bootrom (a108:c309)
# and U-Boot DFU gadget (a108:4d44) are what a camera bench wants to see; set
# RELAY_PANEL_USB_VID to follow a different vendor.
USB_VID_FILTER = os.environ.get("RELAY_PANEL_USB_VID", "a108")

# A write has to land, so it waits. A background read that is going to fail
# should not own the bus while it does: a wedged i2c transfer used to hold the
# lock for the full 5 s, which is what turned a bus glitch into a panel that
# took seconds to answer a click.
BUS_TIMEOUT = 5.0
BUS_BACKGROUND_TIMEOUT = 0.4

SAMPLE_INTERVAL = 1.0      # how often the sampler re-reads the bench
STALE_AFTER = 3.0          # a snapshot older than this is flagged to the UI


class BusLock:
    """Serializes bus access, preferring clicks over background sampling.

    `with bus:` is the foreground path and behaves exactly like the plain Lock
    every caller had before. `with bus.background():` stands aside while any
    foreground caller is waiting, so a poll can never be the reason a relay
    takes a second to switch. Python's Lock is not FIFO-fair, so without this
    a click competes with every in-flight sample and can lose repeatedly.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._waiting = 0
        # Background readers queue among themselves before competing for the
        # bus, so only ever one of them is holding or waiting on _lock. Without
        # it they hand the lock straight to each other and a waiting click sits
        # through all of them rather than just the one already in progress.
        self._bg_turn = threading.Lock()

    def __enter__(self):
        with self._cv:
            self._waiting += 1
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        with self._cv:
            self._waiting -= 1
            if not self._waiting:
                self._cv.notify_all()
        return False

    @contextmanager
    def background(self, patience=2.0):
        """Yield to foreground callers, but not forever.

        The wait is bounded because the bench still has to report what it is
        doing during a long slider drag; starving the sampler outright would
        just trade a slow panel for a lying one.
        """
        deadline = time.monotonic() + patience
        with self._bg_turn:
            # Re-checked here rather than once on the way in: a click that
            # arrives while an earlier sweep is still running must be let
            # through before this one starts, not after it finishes.
            with self._cv:
                self._cv.wait_for(lambda: self._waiting == 0,
                                  timeout=max(0.0, deadline - time.monotonic()))
            with self._lock:
                yield

    def hold(self, background=False):
        """The context manager matching this caller's priority."""
        return self.background() if background else self


def bus_timeout(background):
    """Subprocess timeout matching this caller's priority."""
    return BUS_BACKGROUND_TIMEOUT if background else BUS_TIMEOUT


i2c_lock = BusLock()
gpio_lock = BusLock()
config_lock = threading.Lock()

# Counts writes that have been answered, so a reader can tell whether a
# snapshot is old enough to predate its own click. Without it the panel shows
# a button go green and then grey again, because the poll that lands next may
# be carrying a picture of the bench taken before the relay moved.
write_seq_lock = threading.Lock()
write_seq = 0


def bump_write_seq():
    """Record that a write has been answered. Returns the new sequence."""
    global write_seq
    with write_seq_lock:
        write_seq += 1
        return write_seq


def current_write_seq():
    with write_seq_lock:
        return write_seq


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg.setdefault("boards", [])
    cfg.setdefault("gpios", [])
    cfg.setdefault("pwm", [])
    return cfg


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_tokens():
    """Load the dev-token permission map: sha256(token) -> {"allowed": {...}}.

    Missing or malformed is treated as "no tokens configured" rather than an
    error, so a bench with none set up behaves exactly as it always has.
    """
    if not TOKENS_FILE.exists():
        return {}
    try:
        with open(TOKENS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data.get("tokens", {}) if isinstance(data, dict) else {}


def parse_state(value):
    """Interpret a request's on/off value, refusing anything ambiguous.

    A bare bool() reads the string "off" as true, so a hand-written request
    asking to turn a relay off switched it on instead - the worst available
    reading of the field, on a bench where relays feed camera power. The UI
    and the MCP server both send real booleans; the strings are accepted
    because they are what anyone reaching for curl writes, and everything
    else is a 400 rather than a guess.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("on", "true", "1", "yes"):
            return True
        if v in ("off", "false", "0", "no"):
            return False
    raise ValueError(f"state must be a boolean or one of on/off, got {value!r}")


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def client_is_trusted(addr):
    """Whether a caller may act without presenting a token.

    Going tokenless means full control of the bench, so it is granted by
    where the request came from, never by the request merely declining to
    identify itself. Anything outside TRUSTED_NETS - a VPN guest, say -
    must present a token and is held to that token's scope.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in TRUSTED_NETS)


def i2c_cmd(cmd, bus, addr, *args, timeout=BUS_TIMEOUT):
    """Run an i2c command and return output."""
    full_cmd = [cmd, "-y", str(bus), str(addr)] + [str(a) for a in args]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True,
                                timeout=timeout)
        if result.returncode != 0:
            # i2cget/i2cset report the reason on stderr; returning only stdout
            # left callers printing "write failed:" with nothing after it.
            return (result.stderr.strip() or result.stdout.strip()), result.returncode
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1


# XL9535 register map. The distinction between INPUT and OUTPUT matters more
# than it looks: OUTPUT holds what was last written and echoes it back whatever
# the pins are doing, so reading it back proves only that the chip accepted the
# byte. INPUT reports the actual pin levels, and is the only one of the two
# that can tell you a relay really moved.
XL_INPUT_A = 0x00
XL_OUTPUT_A = 0x02      # power-on default 0xFF - see i2c_init_board
XL_POLARITY_A = 0x04    # inverts the INPUT registers only. Default 0x00.
XL_CONFIG_A = 0x06      # 0 = output. Power-on default is 0xFF: all inputs.


def i2c_write_pair(bus, addr, first, val_a, val_b):
    """Write both registers of a pair in one transaction. Caller holds the lock.

    A second data byte after the command byte lands in the pair's other
    register, so the two ports move together rather than through a state where
    one has been updated and the other has not.
    """
    out, rc = i2c_cmd("i2cset", bus, addr, hex(first), hex(val_a), hex(val_b), "i")
    if rc != 0:
        raise RuntimeError(f"write {first:#04x} at {addr} on bus {bus} failed: {out}")


def i2c_read_pair(bus, addr, first, background=False):
    """Read two consecutive registers in one transaction. Caller holds the lock.

    The XL9535 auto-increments within a register pair, so a single i2ctransfer
    replaces two i2cget subprocesses. On this bench the subprocess is nearly
    all of the cost (5 ms the one way, 9 ms the other), which is what makes
    reading the pins as well as the outputs cheaper than reading the outputs
    alone used to be.
    """
    # r2 and no more: the registers work as pairs and a read past the second
    # byte wraps back to the first, so r4 from 0x02 gives 0x02,0x03,0x02,0x03
    # rather than four consecutive registers.
    cmd = ["i2ctransfer", "-y", str(bus), f"w1@{addr}", hex(first), "r2"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=bus_timeout(background))
    except Exception as e:
        raise RuntimeError(f"read {first:#04x} at {addr} on bus {bus}: {e}")
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 2:
        detail = r.stderr.strip() or r.stdout.strip() or "no data"
        raise RuntimeError(f"read {first:#04x} at {addr} on bus {bus}: {detail}")
    return int(parts[0], 16), int(parts[1], 16)


def i2c_init_board(bus, addr):
    """Bring a board to a known state: every relay off, every pin an output.

    The order is the whole point. Output Port powers up as 0xFF (datasheet
    tables 9 and 10) and a set bit energises a relay here, so configuring the
    pins as outputs first drives all sixteen HIGH until the outputs are
    cleared a transaction later. Measured on the bench: made outputs first,
    the input registers read 0xff, every relay briefly pulled in. Writing the
    outputs while the pins are still high-impedance takes them from undriven
    straight to off, with no pulse at all.

    Polarity inversion is asserted rather than assumed. It inverts the input
    registers, which is exactly what get_board_states compares the outputs
    against, so a stray bit there would make a healthy board report that its
    pins disagree with what it has been told to do.

    All of it is read back, because a write that is merely ACKed proves only
    that the chip heard it.
    """
    with i2c_lock:
        i2c_write_pair(bus, addr, XL_OUTPUT_A, 0x00, 0x00)    # off, still undriven
        i2c_write_pair(bus, addr, XL_POLARITY_A, 0x00, 0x00)  # inputs read straight
        i2c_write_pair(bus, addr, XL_CONFIG_A, 0x00, 0x00)    # only now, drive them
        cfg_a, cfg_b = i2c_read_pair(bus, addr, XL_CONFIG_A)
        pol_a, pol_b = i2c_read_pair(bus, addr, XL_POLARITY_A)
    if cfg_a or cfg_b:
        raise RuntimeError(f"pins did not take output mode "
                           f"(config {cfg_a:#04x}/{cfg_b:#04x}, want 0x00/0x00)")
    if pol_a or pol_b:
        raise RuntimeError(f"polarity inversion did not clear "
                           f"({pol_a:#04x}/{pol_b:#04x}, want 0x00/0x00)")


def i2c_read_state(bus, addr, background=False):
    """Read current output register states. Returns (port_a, port_b) as ints.

    Raises on a failed read rather than defaulting to zero. A wedged bus would
    otherwise be indistinguishable from every relay being off, and on a bench
    where relays feed camera power that is the last thing to report
    confidently. get_full_state turns the exception into the board's `error`.
    """
    with i2c_lock.hold(background):
        try:
            return i2c_read_pair(bus, addr, XL_OUTPUT_A, background)
        except RuntimeError:
            raise RuntimeError(f"no response at {addr} on bus {bus}")


def i2c_read_pins(bus, addr, background=False):
    """Read what the pins are actually doing. Returns (port_a, port_b).

    The cross-check against i2c_read_state: these agree on healthy hardware
    and diverge when a write never reached the pins.
    """
    with i2c_lock.hold(background):
        return i2c_read_pair(bus, addr, XL_INPUT_A, background)


def i2c_write_reg(bus, addr, reg, value):
    """Write a register, raising on failure. Caller holds the bus lock.

    The return code was previously discarded, so a NAK on this one write left
    the API answering with the state it had been asked for while the relay
    stayed put - a click that silently did nothing, which is indistinguishable
    from a click that was never registered.
    """
    out, rc = i2c_cmd("i2cset", bus, addr, reg, hex(value))
    if rc != 0:
        raise RuntimeError(f"write {reg} at {addr} on bus {bus} failed: {out}")


def set_relay(bus, addr, relay_num, state):
    """Set a relay on or off."""
    # Keep the read and write together: another request changing any relay
    # on this port must not have its update overwritten by our old reading.
    with i2c_lock:
        port_a, port_b = i2c_read_pair(bus, addr, XL_OUTPUT_A)
        if relay_num < 8:
            if state:
                port_a |= (1 << relay_num)
            else:
                port_a &= ~(1 << relay_num)
            i2c_write_reg(bus, addr, "0x02", port_a)
        else:
            bit = relay_num - 8
            if state:
                port_b |= (1 << bit)
            else:
                port_b &= ~(1 << bit)
            i2c_write_reg(bus, addr, "0x03", port_b)


def toggle_relay(bus, addr, relay_num):
    """Toggle a relay and return the new state."""
    with i2c_lock:
        port_a, port_b = i2c_read_pair(bus, addr, XL_OUTPUT_A)
        if relay_num < 8:
            port_a ^= (1 << relay_num)
            i2c_write_reg(bus, addr, "0x02", port_a)
            return bool(port_a & (1 << relay_num))
        else:
            bit = relay_num - 8
            port_b ^= (1 << bit)
            i2c_write_reg(bus, addr, "0x03", port_b)
            return bool(port_b & (1 << bit))


def set_all(bus, addr, size, state):
    """Set every relay on a board on or off.

    A 16-way board moves both ports in one transaction, so there is no instant
    where the first eight have switched and the second eight have not. An
    8-way board's second port is left alone rather than zeroed, since nothing
    here knows what else it might be wired to.
    """
    with i2c_lock:
        if size <= 8:
            i2c_write_reg(bus, addr, hex(XL_OUTPUT_A), (1 << size) - 1 if state else 0)
            return
        mask_a = 0xFF if state else 0
        mask_b = ((1 << (size - 8)) - 1) if state else 0
        i2c_write_pair(bus, addr, XL_OUTPUT_A, mask_a, mask_b)


def gpio_cmd(*args, timeout=BUS_TIMEOUT):
    """Run a `gpio` CLI command and return (stdout, returncode)."""
    full_cmd = ["gpio"] + [str(a) for a in args]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True,
                                timeout=timeout)
        if result.returncode != 0:
            return (result.stderr.strip() or result.stdout.strip()), result.returncode
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1


def gpio_init_pin(wpi, initial_state=False):
    """Set a pin to output mode and to the given initial state."""
    with gpio_lock:
        gpio_cmd("mode", wpi, "out")
        gpio_cmd("write", wpi, 1 if initial_state else 0)


def gpio_read(wpi, background=False):
    """Read current state of a pin (True/False)."""
    with gpio_lock.hold(background):
        out, rc = gpio_cmd("read", wpi, timeout=bus_timeout(background))
    if rc != 0:
        raise RuntimeError(f"gpio read {wpi} failed: {out}")
    return out.strip() == "1"


def gpio_write(wpi, state):
    """Set a pin high or low."""
    with gpio_lock:
        out, rc = gpio_cmd("write", wpi, 1 if state else 0)
    if rc != 0:
        raise RuntimeError(f"gpio write {wpi} failed: {out}")


def gpio_toggle_pin(wpi):
    """Toggle a pin and return the new state (read back)."""
    current = gpio_read(wpi)
    gpio_write(wpi, not current)
    return not current


def get_gpio_states(gpios, background=False):
    """Return list of GPIO entries with live state read from hardware."""
    result = []
    for g in gpios:
        wpi = g["wpi"]
        try:
            state = gpio_read(wpi, background=background)
            error = None
        except Exception as e:
            state = False
            error = str(e)
        result.append({
            "wpi": wpi,
            "pin": g.get("pin"),
            "name": g.get("name", f"GPIO {wpi}"),
            "state": "on" if state else "off",
            "error": error,
        })
    return result


# --- PCA9685 16-channel PWM (LED dimming) ---
#
# Drives 12 V LED strips through MOSFET modules for camera day/night testing.
# Registers per the NXP PCA9685 datasheet; the internal 25 MHz oscillator and
# the fixed 4096-step counter give 24 Hz (PRE_SCALE 255) to 1526 Hz
# (PRE_SCALE 3) at a full 12 bits of duty at every frequency.

PCA_MODE1 = 0x00
PCA_MODE2 = 0x01
PCA_LED0_BASE = 0x06      # LED0_ON_L; each channel is 4 consecutive registers
PCA_ALL_LED_BASE = 0xFA   # ALL_LED_ON_L; writes land on all 16 channels
PCA_PRESCALE = 0xFE

PCA_M1_RESTART = 0x80
PCA_M1_EXTCLK = 0x40      # sticky: only a power cycle or SWRST clears it
PCA_M1_AI = 0x20          # auto-increment, required for the 4-byte block write
PCA_M1_SLEEP = 0x10
# SUB1/SUB2/SUB3 and ALLCALL, the addresses the chip answers to besides its
# own. Cleared deliberately: this chip drives a bench's lights, and there is
# nothing to gain from letting a broadcast meant for some other device reach
# it. ALLCALL in particular is enabled at power-up, at 0xE0 (7-bit 0x70).
PCA_M1_EXTRA_ADDRS = 0x0F

# "It takes 500 us max. for the oscillator to be up and running once SLEEP bit
# has been set to logic 0. Timings on LEDn outputs are not guaranteed if PWM
# control registers are accessed within the 500 us window." Every i2c op here
# spawns a subprocess and so already takes longer than that, but relying on
# that is relying on an accident: a future switch to a persistent bus handle
# would silently start writing inside the window.
PCA_OSC_SETTLE = 0.001

PCA_OSC_HZ = 25_000_000
PCA_STEPS = 4096
PCA_CHANNELS = 16         # the chip always has 16; config picks which are wired
PCA_FULL = 0x10           # bit 4 of LEDn_ON_H / LEDn_OFF_H
PWM_MAX = PCA_STEPS - 1   # 4095
PCA_PRESCALE_MIN = 3      # 1526.0 Hz
PCA_PRESCALE_MAX = 255    # 23.8 Hz
PWM_DEFAULT_FREQ = 1526


def pca_prescale_for(freq_hz):
    """Nearest achievable PRE_SCALE for a requested frequency."""
    ps = int(round(PCA_OSC_HZ / (PCA_STEPS * float(freq_hz)))) - 1
    return max(PCA_PRESCALE_MIN, min(PCA_PRESCALE_MAX, ps))


def pca_freq_for(prescale):
    """Frequency the chip actually produces at a given PRE_SCALE."""
    return PCA_OSC_HZ / (PCA_STEPS * (int(prescale) + 1))


def pca_words(level, phase):
    """Build the 4 channel registers for `level` counts starting at `phase`.

    The 12-bit masking matters: writing an unmasked 4096 puts a 1 in bit 4 of
    the high byte, which is the FULL_ON / FULL_OFF flag, so a "100%" that was
    computed as 4096 silently latches the channel instead of setting a duty.
    The extremes are expressed with those flags deliberately, which is also how
    0% and 100% avoid emitting a 160 ns sliver of a pulse.
    """
    level = max(0, min(PWM_MAX, int(level)))
    if level == 0:
        return [0x00, 0x00, 0x00, PCA_FULL]      # FULL_OFF
    if level == PWM_MAX:
        return [0x00, PCA_FULL, 0x00, 0x00]      # FULL_ON
    on = int(phase) & 0x0FFF
    off = (on + level) & 0x0FFF                  # wraps; the chip handles off < on
    return [on & 0xFF, (on >> 8) & 0x0F, off & 0xFF, (off >> 8) & 0x0F]


def pca_level(words):
    """Recover the duty in counts from 4 channel registers."""
    on_l, on_h, off_l, off_h = words
    if off_h & PCA_FULL:                         # FULL_OFF wins over FULL_ON
        return 0
    if on_h & PCA_FULL:
        return PWM_MAX
    on = ((on_h & 0x0F) << 8) | on_l
    off = ((off_h & 0x0F) << 8) | off_l
    return (off - on) & 0x0FFF


def pca_dump(bus, addr, background=False):
    """Read all 256 registers in a single i2cdump. Returns a list or None.

    One subprocess per poll instead of 64 i2cget calls for a 16-channel read.
    """
    with i2c_lock.hold(background):
        out, rc = i2c_cmd("i2cdump", bus, addr, "b",
                          timeout=bus_timeout(background))
    if rc != 0:
        return None
    regs = [0] * 256
    seen = 0
    for line in out.splitlines():
        parts = line.split()
        if not parts or not parts[0].endswith(":"):
            continue
        try:
            base = int(parts[0][:-1], 16)
        except ValueError:
            continue
        for i, tok in enumerate(parts[1:17]):    # stop before the ASCII column
            if len(tok) == 2 and base + i < 256:
                try:
                    regs[base + i] = int(tok, 16)
                    seen += 1
                except ValueError:
                    pass                          # "XX" = unreadable register
    # i2cdump exits 0 even when nothing answers, printing a full page of XX, so
    # an empty parse is how an absent chip actually shows up. Writes NAK and
    # report themselves; reads would otherwise look like a chip full of zeros.
    return regs if seen else None


def pca_set_channel(bus, addr, ch, level, phase):
    """Set one channel's duty. Returns the level actually written."""
    words = pca_words(level, phase)
    reg = PCA_LED0_BASE + 4 * int(ch)
    with i2c_lock:
        out, rc = i2c_cmd("i2cset", bus, addr, hex(reg),
                          *(hex(w) for w in words), "i")
    if rc != 0:
        raise RuntimeError(f"i2cset ch{ch} failed: {out}")
    return pca_level(words)


def pca_all_off(bus, addr):
    """Force every channel FULL_OFF in one transaction."""
    with i2c_lock:
        out, rc = i2c_cmd("i2cset", bus, addr, hex(PCA_ALL_LED_BASE),
                          "0x00", "0x00", "0x00", hex(PCA_FULL), "i")
    if rc != 0:
        raise RuntimeError(f"pwm all-off failed: {out}")


def pca_set_freq(bus, addr, freq_hz):
    """Change PWM frequency. Returns (prescale, achieved_hz).

    PRE_SCALE is only writable while asleep, so this drops the outputs for
    about a millisecond and then RESTART resumes them at their prior duty.
    """
    ps = pca_prescale_for(freq_hz)
    with i2c_lock:
        cur, rc = i2c_cmd("i2cget", bus, addr, hex(PCA_MODE1))
        mode1 = int(cur, 16) if rc == 0 and cur.startswith("0x") else PCA_M1_AI
        # Carry the chip's own MODE1 forward, but never its auto-increment bit.
        # A chip that reset to defaults reads back AI=0, and preserving that
        # would leave every later 4-byte channel write collapsing onto
        # LEDn_ON_L, so forcing it on here makes a frequency change a repair.
        base = (mode1 | PCA_M1_AI) & ~PCA_M1_RESTART & 0xFF
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1), hex(base | PCA_M1_SLEEP))
        i2c_cmd("i2cset", bus, addr, hex(PCA_PRESCALE), hex(ps))
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1), hex(base & ~PCA_M1_SLEEP))
        time.sleep(PCA_OSC_SETTLE)               # oscillator start-up
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1),
                hex((base & ~PCA_M1_SLEEP) | PCA_M1_RESTART))
    return ps, pca_freq_for(ps)


def pca_set_mode1_ai(bus, addr, keep=0):
    """Set MODE1 with auto-increment on, preserving `keep`. Returns the readback.

    Every channel write is a 4-byte block starting at LEDn_ON_L, which only
    lands on four registers while AI is set; with AI clear the chip drops all
    four bytes onto LEDn_ON_L alone, so the FULL_OFF flag in LEDn_OFF_H is
    never cleared and the channel stays dark. The write is still ACKed, so
    nothing downstream can tell - which is why this reads MODE1 back rather
    than trusting the write.
    """
    want = ((keep | PCA_M1_AI)
            & ~PCA_M1_SLEEP & ~PCA_M1_RESTART & ~PCA_M1_EXTRA_ADDRS & 0xFF)
    with i2c_lock:
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1), hex(want))
        # Clearing SLEEP starts the oscillator; hold off the callers that go
        # straight on to write PWM registers until it is actually running.
        time.sleep(PCA_OSC_SETTLE)
        cur, rc = i2c_cmd("i2cget", bus, addr, hex(PCA_MODE1))
    if rc != 0 or not cur.startswith("0x"):
        raise RuntimeError(f"MODE1 readback failed at {addr} on bus {bus}: {cur}")
    return int(cur, 16)


def pca_extclk_stuck(regs):
    """Whether the chip has been switched to an external clock it hasn't got.

    EXTCLK is a sticky bit: writing 0 does not clear it, only a power cycle or
    an I2C software reset does. So this is called out separately from
    pca_needs_init - re-initializing cannot fix it, and a poll that kept
    trying would just re-run init every second forever. While it is set the
    frequency this panel reports is computed from an internal oscillator the
    chip is no longer using.
    """
    return bool(regs and regs[PCA_MODE1] & PCA_M1_EXTCLK)


def pca_needs_init(regs, want_prescale):
    """Whether the chip is at power-on defaults rather than at our setup.

    SLEEP set is the giveaway that it has just been powered up, since the
    panel never leaves it asleep. Auto-increment clear is the one that bites
    hardest, because writes still ACK without it and the strips simply stay
    dark. A stale PRE_SCALE catches a reset that somehow woke.
    """
    if regs[PCA_MODE1] & PCA_M1_SLEEP:
        return True
    if not regs[PCA_MODE1] & PCA_M1_AI:
        return True
    return regs[PCA_PRESCALE] != want_prescale


def pca_init(bus, addr, freq_hz, attempts=3):
    """Wake the chip, enable auto-increment, set frequency, all channels off.

    Retried because a single write here has been seen to fail at startup on an
    otherwise healthy bus, and giving up on one NAK leaves the chip asleep at
    the wrong frequency with auto-increment clear. The poll's repair only
    covers auto-increment, so the rest has to be got right here.
    """
    for attempt in range(1, attempts + 1):
        try:
            pca_init_once(bus, addr, freq_hz)
            return
        except RuntimeError as e:
            if attempt == attempts:
                raise
            print(f"  PWM {addr} on bus {bus}: init attempt {attempt} "
                  f"failed ({e}), retrying")
            time.sleep(0.2)


def pca_init_once(bus, addr, freq_hz):
    """One attempt at the init sequence. Raises on any failed write."""
    with i2c_lock:
        # totem-pole non-inverting: output high turns a MOSFET module's gate on
        out, rc = i2c_cmd("i2cset", bus, addr, hex(PCA_MODE2), "0x04")
    if rc != 0:
        raise RuntimeError(f"pwm MODE2 write failed: {out}")
    # Verified, not assumed: an unchecked MODE1 write that NAKs during a bus
    # wobble leaves AI clear for the life of the process, and from then on
    # every set reports success while the strips stay dark.
    mode1 = pca_set_mode1_ai(bus, addr)
    if not mode1 & PCA_M1_AI:
        raise RuntimeError(f"pwm auto-increment did not stick (MODE1={mode1:#04x})")
    pca_all_off(bus, addr)
    # RESTART at the end of pca_set_freq resumes the pre-sleep duty, so the
    # all-off above is what makes the restart land on "dark" rather than on
    # whatever was lit before.
    pca_set_freq(bus, addr, freq_hz)


def pca_enabled_channels(chip):
    """Channel numbers this chip manages, in order.

    The `channels` map is the source of truth, so channels can be added and
    removed one at a time: a rig might wire 0-3 for two strips and later 7 for
    an IR bank, with nothing in between. `size` is only consulted for configs
    written before per-channel add/remove existed.
    """
    raw = chip.get("channels")
    if isinstance(raw, dict) and raw:
        out = set()
        for k in raw:
            try:
                c = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= c < PCA_CHANNELS:
                out.add(c)
        return sorted(out)
    size = min(int(chip.get("size", PCA_CHANNELS)), PCA_CHANNELS)
    return list(range(max(0, size)))


def pca_channel_cfg(chip, ch):
    """(name, phase) for a channel, filling in defaults."""
    entry = chip.get("channels", {}).get(str(ch)) or {}
    if isinstance(entry, str):
        entry = {"name": entry}
    name = entry.get("name") or f"Ch {ch}"
    phase = entry.get("phase")
    if phase is None:
        # Quarter-period stagger. Channels lit together then spread their
        # on-times across the cycle instead of stacking at count 0, which
        # lowers both the peak current draw and the combined optical ripple.
        phase = (ch * 1024) & 0x0FFF
    return name, int(phase) & 0x0FFF


def pca_group_cfg(chip, gi):
    """Normalized (name, channels, weights) for one channel group."""
    groups = chip.get("groups", [])
    g = groups[gi] if 0 <= gi < len(groups) else {}
    name = g.get("name") or f"Group {gi}"
    # A group can only cover channels the chip still manages, so removing a
    # channel cannot leave a group pointing at one that is no longer there.
    enabled = set(pca_enabled_channels(chip))
    channels = []
    for c in g.get("channels", []):
        try:
            c = int(c)
        except (TypeError, ValueError):
            continue
        if c in enabled and c not in channels:
            channels.append(c)
    raw = g.get("weights") or {}
    weights = {}
    for ch in channels:
        try:
            w = int(raw.get(str(ch), PWM_MAX))
        except (TypeError, ValueError):
            w = PWM_MAX
        weights[ch] = max(0, min(PWM_MAX, w))
    return name, channels, weights


def group_levels_for(channels, weights, master):
    """Member duties for a master level.

    Weights all default to full, which makes a group plainly "set every member
    to the same level". Setting them unequal instead holds a calibrated colour
    balance while the master dims, which is what an RGB strip standing in for
    white light needs.
    """
    master = max(0, min(PWM_MAX, int(master)))
    return {ch: int(round(weights[ch] * master / PWM_MAX)) for ch in channels}


def group_master_from(channels, weights, levels):
    """Recover the master from the hardware, referenced to the heaviest member.

    Derived rather than stored. A channel nudged on its own can never leave a
    stale master behind, and dragging back up from zero still recovers the
    balance because the ratios live in the weights, not in the last levels
    written.

    The heaviest member is the reference because scaling a small level back up
    multiplies its rounding error by the inverse of its weight: a member at a
    quarter weight turns half a count of quantisation into two counts of
    master, which then reads back as a false "mixed".
    """
    ref, ref_w = None, 0
    for ch in channels:
        w = weights.get(ch, 0)
        if w > ref_w:
            ref, ref_w = ch, w
    if ref is None or ref_w <= 0:
        return 0
    return min(PWM_MAX, int(round(levels.get(ref, 0) * PWM_MAX / ref_w)))


def get_pwm_groups(chip, levels):
    """Group state for a chip, derived from the channel levels just read."""
    groups = []
    for gi in range(len(chip.get("groups", []))):
        name, channels, weights = pca_group_cfg(chip, gi)
        master = group_master_from(channels, weights, levels)
        expected = group_levels_for(channels, weights, master)
        # A member off its weighted ratio means someone set a channel by hand;
        # surfacing that beats silently showing a master that is not the truth.
        mixed = any(abs(levels.get(ch, 0) - expected[ch]) > 1 for ch in channels)
        groups.append({
            "index": gi,
            "name": name,
            "channels": channels,
            "weights": {str(k): v for k, v in weights.items()},
            "level": master,
            "percent": round(100.0 * master / PWM_MAX, 2),
            "mixed": mixed,
        })
    return groups


def get_pwm_state(config, background=False):
    """Live state of every configured PWM chip, read back from hardware."""
    result = []
    for idx, chip in enumerate(config.get("pwm", [])):
        bus = chip["bus"]
        addr = chip["address"]
        enabled = pca_enabled_channels(chip)
        regs = pca_dump(bus, addr, background=background)
        want_hz = chip.get("freq_hz", PWM_DEFAULT_FREQ)
        # The chip is powered through the relay boards this panel drives, so
        # it is absent at startup by construction (init_boards has just forced
        # every relay off, master rails included) and reappears mid-session
        # when someone switches the master back on. Setting it up therefore
        # belongs to whoever next finds it at power-on defaults, not to a
        # startup step that only ever gets one attempt at a dead chip.
        chip_error = None if regs else f"no response at {addr} on bus {bus}"
        if pca_extclk_stuck(regs):
            chip_error = (f"MODE1 EXTCLK is set ({regs[PCA_MODE1]:#04x}): the chip "
                          "is waiting on an external clock, so PWM is stopped and "
                          "the frequency shown is meaningless. Sticky bit - only a "
                          "power cycle of the rail clears it.")
        elif regs and pca_needs_init(regs, pca_prescale_for(want_hz)):
            try:
                pca_init(bus, addr, want_hz)
                print(f"  PWM {addr} on bus {bus}: initialized "
                      f"(was MODE1 {regs[PCA_MODE1]:#04x}, "
                      f"PRE_SCALE {regs[PCA_PRESCALE]})")
                regs = pca_dump(bus, addr, background=background) or regs
            except Exception as e:
                print(f"  PWM {addr} on bus {bus}: init failed: {e}")
        prescale = regs[PCA_PRESCALE] if regs else 0
        channels = []
        for ch in enabled:
            name, phase = pca_channel_cfg(chip, ch)
            if regs:
                base = PCA_LED0_BASE + 4 * ch
                level = pca_level(regs[base:base + 4])
            else:
                level = 0
            channels.append({
                "ch": ch,
                "name": name,
                "phase": phase,
                "level": level,
                "percent": round(100.0 * level / PWM_MAX, 2),
            })
        levels = {c["ch"]: c["level"] for c in channels}
        result.append({
            "index": idx,
            "name": chip.get("name", f"PWM {idx}"),
            "address": addr,
            "bus": bus,
            "size": len(enabled),
            # channels the chip has but this config does not manage, so the UI
            # can offer exactly what is left to add
            "available": [c for c in range(PCA_CHANNELS) if c not in enabled],
            "prescale": prescale,
            "freq_hz": round(pca_freq_for(prescale), 1) if regs else 0,
            "channels": channels,
            "groups": get_pwm_groups(chip, levels),
            "error": chip_error,
        })
    return result


def i2c_address_conflict(config, bus, addr, skip_pwm=None):
    """Return a description if something else already claims this bus address.

    Worth blocking rather than trusting the user: a PCA9685 write aimed at an
    XL9535 relay board lands on its output registers and power-cycles whatever
    is plugged into that board.
    """
    for b in config.get("boards", []):
        if b["address"] == addr and b["bus"] == bus:
            return f"{addr} on bus {bus} is relay board '{b.get('name', '?')}'"
    for i, c in enumerate(config.get("pwm", [])):
        if i != skip_pwm and c["address"] == addr and c["bus"] == bus:
            return f"{addr} on bus {bus} is PWM chip '{c.get('name', '?')}'"
    return None


def pca_materialize_channels(chip):
    """Turn a legacy `size`-only chip into an explicit channel map.

    Add and remove operate on the map, so anything written before per-channel
    control existed has to be expanded once before it can be edited.
    """
    existing = pca_enabled_channels(chip)
    chans = chip.setdefault("channels", {})
    for c in existing:
        chans.setdefault(str(c), {"name": f"Ch {c}"})
    chip.pop("size", None)
    return chans


def resolve_level(body):
    """Pull a duty out of a request body: `level` (0-4095) or `percent`."""
    if body.get("level") is not None:
        return max(0, min(PWM_MAX, int(body["level"])))
    if body.get("percent") is not None:
        pct = float(body["percent"])
        return max(0, min(PWM_MAX, int(round(pct * PWM_MAX / 100.0))))
    return None


def read_usb_attr(dev_dir, name):
    """Read a sysfs attribute, returning None if absent/unreadable."""
    try:
        return (dev_dir / name).read_text().strip()
    except OSError:
        return None


def get_usb_devices(vid=USB_VID_FILTER):
    """List connected USB devices whose idVendor matches vid."""
    devices = []
    try:
        entries = sorted(p for p in USB_SYS_PATH.iterdir() if ":" not in p.name)
    except OSError:
        return devices
    for dev_dir in entries:
        if read_usb_attr(dev_dir, "idVendor") != vid:
            continue
        devices.append({
            "port": dev_dir.name,
            "vid": vid,
            "pid": read_usb_attr(dev_dir, "idProduct"),
            "product": read_usb_attr(dev_dir, "product"),
            "manufacturer": read_usb_attr(dev_dir, "manufacturer"),
            "serial": read_usb_attr(dev_dir, "serial"),
            "busnum": read_usb_attr(dev_dir, "busnum"),
            "devnum": read_usb_attr(dev_dir, "devnum"),
        })
    return devices


def pin_mismatch(size, out_a, out_b, in_a, in_b):
    """Describe any disagreement between commanded outputs and actual pins.

    Only the bits this board actually has are compared; a 16-way board uses
    both ports, an 8-way one leaves port B undefined and it must not be read
    as sixteen relays worth of fault.
    """
    mask_a = (1 << min(size, 8)) - 1
    mask_b = (1 << max(0, size - 8)) - 1
    if not ((out_a ^ in_a) & mask_a or (out_b ^ in_b) & mask_b):
        return None
    return (f"pins disagree with commanded outputs "
            f"(commanded {out_a:#04x}/{out_b:#04x}, pins {in_a:#04x}/{in_b:#04x})")


def get_board_states(board, background=False):
    """Relay states for a board, plus any disagreement with the actual pins.

    Returns (states, warning). `states` comes from the output registers, i.e.
    what the board has been commanded to do, because that is what a
    read-modify-write has to build on: deriving the other fifteen relays from
    a measurement would let one misread pin move every relay on the board.

    `warning` is the cross-check the output registers cannot give you, since
    they echo whatever was written whether or not it reached a pin. The two
    agree on healthy hardware, and diverge on exactly the failures that are
    otherwise invisible - a board still in its power-on all-inputs state, or a
    pin that is not following its register.
    """
    bus, addr, size = board["bus"], board["address"], board["size"]
    out_a, out_b = i2c_read_state(bus, addr, background=background)
    states = {}
    for i in range(size):
        states[i] = bool((out_a if i < 8 else out_b) & (1 << (i % 8)))
    try:
        in_a, in_b = i2c_read_pins(bus, addr, background=background)
    except RuntimeError:
        # The pin read is a cross-check, not the state itself. Losing it costs
        # the warning, and must not cost the reading we already have.
        return states, None
    return states, pin_mismatch(size, out_a, out_b, in_a, in_b)


def get_full_state(config, background=False):
    """Get the full state of all boards and relays."""
    result = []
    for idx, board in enumerate(config["boards"]):
        try:
            states, error = get_board_states(board, background=background)
        except Exception as e:
            states = {i: False for i in range(board["size"])}
            error = str(e)
        relays = []
        for i in range(board["size"]):
            name = board.get("relays", {}).get(str(i), f"Relay {i}")
            relays.append({
                "num": i,
                "name": name,
                "state": "on" if states.get(i, False) else "off",
            })
        result.append({
            "index": idx,
            "name": board.get("name", f"Board {idx}"),
            "address": board["address"],
            "bus": board["bus"],
            "size": board["size"],
            "layout": board.get("layout", "grid"),
            "flipped": board.get("flipped", False),
            "relays": relays,
            "error": error,
        })
    return result


class BenchSampler:
    """One background thread reads the bench; every client serves its snapshot.

    Reading hardware on the request path meant each open tab put a full sweep
    of the bus (about ten subprocesses, ~150 ms of lock) between a click and
    the relay it was meant to switch, and the cost scaled with the number of
    tabs. One sampler decouples the two: readers cost no bus time at all, and
    a click contends only with other clicks.

    The snapshot is served with its age rather than as bare truth. A wedged
    bus has to show up as stale data, not as a confident report of a bench
    nobody has actually looked at - the same reason i2c_read_state raises
    instead of reporting a failed read as "all off".
    """

    def __init__(self, interval=SAMPLE_INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._snapshot = None
        self._at = 0.0
        self._covers = 0
        self._sample_seq = 0
        self._published_seq = 0

    def sample(self, background=True):
        """One full read of the bench."""
        config = load_config()
        return {
            "boards": get_full_state(config, background=background),
            "gpios": get_gpio_states(config.get("gpios", []),
                                     background=background),
            "pwm": get_pwm_state(config, background=background),
            "usb": get_usb_devices(),
            # so the UI can label the section with the vendor actually
            # being watched rather than assuming the default
            "usb_vid": USB_VID_FILTER,
        }

    def _begin_sample(self):
        with self._lock:
            self._sample_seq += 1
            return self._sample_seq

    def _publish(self, snap, covers, sample_seq):
        with self._lock:
            # A slow background sweep may finish after a newer foreground
            # read. It must not replace that newer observation.
            if sample_seq < self._published_seq:
                return
            self._snapshot = dict(snap, sample_seq=sample_seq)
            self._at = time.monotonic()
            self._covers = covers
            self._published_seq = sample_seq

    def latest(self):
        """(snapshot, age, writes covered), or (None, None, 0) before the first read."""
        with self._lock:
            if self._snapshot is None:
                return None, None, 0
            return self._snapshot, time.monotonic() - self._at, self._covers

    def fresh(self):
        """Read hardware now, at foreground priority, and publish it."""
        # Captured before the read, not after: a write landing mid-sweep may
        # have missed the board already visited, so only writes finished
        # before the sweep began can be claimed.
        sample_seq = self._begin_sample()
        covers = current_write_seq()
        snap = self.sample(background=False)
        self._publish(snap, covers, sample_seq)
        return snap

    def payload(self, fresh=False):
        """The /api/state body, with the reading's age and what it covers."""
        snap, age, covers = (None, None, 0) if fresh else self.latest()
        if snap is None:
            self.fresh()
            snap, age, covers = self.latest()
        out = dict(snap)
        out["sampled_age_s"] = round(age, 2)
        out["stale"] = age > STALE_AFTER
        # Lets a client tell whether this picture is old enough to predate a
        # write it has already been told succeeded.
        out["covers_writes"] = covers
        return out

    def nudge(self):
        """Resample as soon as possible, because something just changed.

        Writes are human-paced and the Event coalesces, so a slider drag costs
        one extra sweep rather than one per message.
        """
        self._wake.set()

    def run(self):
        while True:
            try:
                sample_seq = self._begin_sample()
                covers = current_write_seq()
                self._publish(self.sample(), covers, sample_seq)
            except Exception as e:
                # Keep serving the last good snapshot; its age is what tells
                # the UI something is wrong. Inventing an all-off bench here
                # would be worse than admitting the reading is old.
                print(f"  sampler: {e}")
            self._wake.wait(self.interval)
            self._wake.clear()

    def start(self):
        threading.Thread(target=self.run, name="bench-sampler",
                         daemon=True).start()


sampler = BenchSampler()


class RelayHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the relay control panel."""

    # Drop a client that stalls mid-request instead of holding its thread
    # open indefinitely - a backgrounded mobile browser on a slow link will
    # otherwise sit in the middle of a request for as long as it likes.
    timeout = 30

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def send_json(self, data, status=200, changes_bench=True):
        if isinstance(data, dict):
            data = dict(data, server_id=SERVER_ID)
        # Every mutating endpoint answers through here, so this is the one
        # place that reliably knows the bench changed. Nudging centrally beats
        # remembering to do it in each of ~30 handlers, where a new endpoint
        # would silently forget and leave the snapshot a second behind.
        # UART input moves nothing the sampler reads; nudging for it would
        # cost a full bus sweep, PWM dump included, for every keystroke.
        if changes_bench and self.command in ("POST", "PUT", "DELETE") and status < 400:
            # Counted before the sampler is woken, so the sweep it triggers
            # captures a sequence that already includes this write. Nudging
            # first would have the resample claim to cover less than it does,
            # and the client would sit on its pending state for an extra
            # poll waiting for a number that had already gone by.
            seq = bump_write_seq()
            sampler.nudge()
            # Stamped here for the same reason the nudge is: every mutating
            # endpoint answers through this one place, so none can forget to
            # tell the caller which write its next poll has to catch up to.
            if isinstance(data, dict):
                data = dict(data, write_seq=seq)
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _token_scope(self):
        """Resolve the Authorization header to an access scope.

        Returns (True, None) when unrestricted - no token presented, from a
        trusted source - (True, allowed) for a recognized token limited to
        `allowed`, or (False, None) for a header that names no known token,
        or for an untrusted caller presenting none at all.
        """
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return (True, None) if client_is_trusted(self.client_address[0]) else (False, None)
        token = auth[len("Bearer "):].strip()
        entry = load_tokens().get(hash_token(token))
        if entry is None:
            return False, None
        return True, entry.get("allowed", {})

    def forbidden(self):
        self.send_json({"error": "forbidden"}, 403)

    def authorize_relay(self, board, relay):
        """One relay. Also covers renaming a relay the token already controls."""
        ok, allowed = self._token_scope()
        if not ok:
            return False
        if allowed is None:
            return True
        return any(e.get("board") == board and e.get("relay") == relay
                    for e in allowed.get("relays", []))

    def authorize_board_bulk(self, board, size):
        """Whole-board actions (all/reset): every relay on it must be granted."""
        ok, allowed = self._token_scope()
        if not ok:
            return False
        if allowed is None:
            return True
        granted = {e.get("relay") for e in allowed.get("relays", [])
                   if e.get("board") == board}
        return granted.issuperset(range(size))

    def authorize_gpio(self, wpi):
        ok, allowed = self._token_scope()
        if not ok:
            return False
        if allowed is None:
            return True
        return wpi in allowed.get("gpios", [])

    def authorize_pwm_channel(self, chip, channel):
        ok, allowed = self._token_scope()
        if not ok:
            return False
        if allowed is None:
            return True
        return any(e.get("chip") == chip and e.get("channel") == channel
                    for e in allowed.get("pwm", []))

    def authorize_pwm_channels(self, chip, channels):
        """Multi-channel actions (all/off/group): every channel touched must be granted."""
        ok, allowed = self._token_scope()
        if not ok:
            return False
        if allowed is None:
            return True
        granted = {e.get("channel") for e in allowed.get("pwm", [])
                   if e.get("chip") == chip}
        return granted.issuperset(channels)

    def authorize_owner_only(self):
        """Structural/config actions (add, remove, rescope, move, resize, ...).

        These reshape the bench itself rather than operate a granted device,
        so no token allow-list can grant them - only unrestricted (no token)
        access can.
        """
        ok, allowed = self._token_scope()
        if not ok:
            return False
        return allowed is None

    def allowed_uarts(self):
        ok, allowed = self._token_scope()
        if not ok:
            return set()
        if allowed is None:
            return set(range(UART_COUNT))
        return {index for index in allowed.get("uarts", [])
                if type(index) is int and 0 <= index < UART_COUNT}

    def allowed_uart_input(self):
        """UARTs the caller may type into: a separate grant on top of viewing,
        so a token that only lists `uarts` stays read only."""
        ok, allowed = self._token_scope()
        if not ok:
            return set()
        if allowed is None:
            return set(range(UART_COUNT))
        return {index for index in allowed.get("uart_input", [])
                if type(index) is int and 0 <= index < UART_COUNT} & self.allowed_uarts()

    def uart_input(self, body):
        # Keystrokes reach a root shell on a bench device, so a cross-site
        # page must not be able to send them. A JSON content type forces a
        # CORS preflight this server never answers, and a foreign Origin is
        # refused outright. Neither stops DNS rebinding.
        if self.headers.get_content_type() != "application/json":
            self.send_json({"error": "Content-Type must be application/json"}, 415)
            return
        origin = self.headers.get("Origin")
        if origin is not None and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
            self.forbidden()
            return
        if not isinstance(body, dict):
            self.send_json({"error": "Expected a JSON object"}, 400)
            return
        index, data = body.get("uart"), body.get("data")
        if type(index) is not int or not 0 <= index < UART_COUNT:
            self.send_json({"error": "Invalid UART"}, 400)
            return
        if not isinstance(data, str) or not data:
            self.send_json({"error": "data must be a non-empty string"}, 400)
            return
        if index not in self.allowed_uart_input():
            self.forbidden()
            return
        raw = data.encode("utf-8", "replace")
        if len(raw) > UART_INPUT_MAX:
            self.send_json({"error": f"At most {UART_INPUT_MAX} bytes per request"}, 413)
            return
        try:
            uart_hub.write(index, raw)
        except UartNotConnected:
            self.send_json({"error": f"ttyUSB{index} is not connected; open its console first"}, 409)
            return
        except OSError as error:
            self.send_json({"error": f"ttyUSB{index} input failed: {error}"}, 503)
            return
        self.send_json({"ok": True, "uart": index, "bytes": len(raw)}, changes_bench=False)

    def stream_uart(self, selection):
        allowed = self.allowed_uarts()
        if not allowed:
            self.forbidden()
            return
        if selection == "all":
            indices = allowed
        else:
            try:
                index = int(selection)
            except (TypeError, ValueError):
                self.send_json({"error": "Invalid UART"}, 400)
                return
            if not 0 <= index < UART_COUNT:
                self.send_json({"error": "Invalid UART"}, 400)
                return
            if index not in allowed:
                self.forbidden()
                return
            indices = {index}
        try:
            subscription = uart_hub.subscribe(indices)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, 503)
            return

        def send_event(event, data):
            body = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(body)
            self.wfile.flush()

        try:
            self.connection.settimeout(10)
            # Echo of typed input arrives as many tiny events; Nagle would
            # hold each one back waiting on the ACK for the last.
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            send_event("ready", {"uarts": sorted(indices), "server_id": SERVER_ID})
            check_at = time.monotonic() + UART_HEARTBEAT
            while True:
                now = time.monotonic()
                if now >= check_at:
                    if not indices.issubset(self.allowed_uarts()):
                        send_event("denied", {"message": "UART access changed; reconnect with an authorized token"})
                        return
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    check_at = now + UART_HEARTBEAT
                if subscription.overflow.is_set():
                    send_event("gap", {"message": "Viewer fell behind; some output was dropped. Reconnecting."})
                    return
                try:
                    event, data = subscription.events.get(timeout=max(0.01, check_at - time.monotonic()))
                except queue.Empty:
                    continue
                send_event(event, data)
        except (OSError, ConnectionError):
            pass  # Browser closed/switched consoles, or stopped reading.
        finally:
            uart_hub.unsubscribe(subscription)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/uarts":
            allowed = self.allowed_uarts()
            if not allowed and UART_COUNT:
                self.forbidden()
                return
            writable = self.allowed_uart_input()
            self.send_json({"uarts": [{"index": index, "name": f"ttyUSB{index}",
                                      "port": UART_BASE_PORT + index, "input": index in writable}
                                     for index in sorted(allowed)], "read_only": not writable})
            return

        if path == "/api/uart/stream":
            self.stream_uart(parse_qs(parsed.query).get("uart", [""])[0])
            return

        if path == "/api/state":
            # `fresh=1` reads hardware instead of the snapshot, for callers
            # that must see the bench rather than a recent picture of it -
            # deploy.py verifying a restore actually landed, say.
            want_fresh = parse_qs(parsed.query).get("fresh", ["0"])[0] not in ("0", "")
            self.send_json(sampler.payload(fresh=want_fresh))
            return

        if path == "/api/pwm":
            snap = sampler.payload()
            self.send_json({"pwm": snap["pwm"],
                            "sampled_age_s": snap["sampled_age_s"],
                            "stale": snap["stale"]})
            return

        if path == "/api/gpios":
            snap = sampler.payload()
            self.send_json({"gpios": snap["gpios"],
                            "sampled_age_s": snap["sampled_age_s"],
                            "stale": snap["stale"]})
            return

        if path == "/api/usb":
            self.send_json({"usb": get_usb_devices(), "vid": USB_VID_FILTER})
            return

        # Serve static files
        if path == "" or path == "/":
            path = "/index.html"
        try:
            # Resolve both traversal components and symlinks before checking
            # containment. A shared string prefix is not a directory boundary.
            static_root = STATIC_DIR.resolve()
            path = unquote(path)
            file_path = (static_root / path.lstrip("/")).resolve()
            file_path.relative_to(static_root)
        except (OSError, RuntimeError, ValueError):
            self.send_response(404)
            self.end_headers()
            return
        if file_path.exists() and file_path.is_file():
            content = file_path.read_bytes()
            content_type = "text/html"
            if path.endswith(".js"):
                content_type = "application/javascript"
            elif path.endswith(".css"):
                content_type = "text/css"
            elif path.endswith(".png"):
                content_type = "image/png"
            elif path.endswith(".svg"):
                content_type = "image/svg+xml"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self.read_body()

        if path == "/api/uart/input":
            self.uart_input(body)
            return

        if path == "/api/relay/toggle":
            board_idx = body.get("board", 0)
            relay_num = body.get("relay", 0)
            if not self.authorize_relay(board_idx, relay_num):
                self.forbidden()
                return
            config = load_config()
            if board_idx >= len(config["boards"]):
                self.send_json({"error": "Invalid board"}, 400)
                return
            board = config["boards"][board_idx]
            try:
                new_state = toggle_relay(board["bus"], board["address"], relay_num)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            relay_name = board.get("relays", {}).get(str(relay_num), f"Relay {relay_num}")
            self.send_json({
                "relay": relay_num,
                "name": relay_name,
                "state": "on" if new_state else "off",
                "board": board_idx,
            })

        elif path == "/api/relay/set":
            board_idx = body.get("board", 0)
            relay_num = body.get("relay", 0)
            try:
                state = parse_state(body.get("state", False))
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            if not self.authorize_relay(board_idx, relay_num):
                self.forbidden()
                return
            config = load_config()
            if board_idx >= len(config["boards"]):
                self.send_json({"error": "Invalid board"}, 400)
                return
            board = config["boards"][board_idx]
            try:
                set_relay(board["bus"], board["address"], relay_num, state)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            relay_name = board.get("relays", {}).get(str(relay_num), f"Relay {relay_num}")
            self.send_json({
                "relay": relay_num,
                "name": relay_name,
                "state": "on" if state else "off",
                "board": board_idx,
            })

        elif path == "/api/board/all":
            board_idx = body.get("board", 0)
            try:
                state = parse_state(body.get("state", False))
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            config = load_config()
            if board_idx >= len(config["boards"]):
                self.send_json({"error": "Invalid board"}, 400)
                return
            board = config["boards"][board_idx]
            if not self.authorize_board_bulk(board_idx, board["size"]):
                self.forbidden()
                return
            set_all(board["bus"], board["address"], board["size"], state)
            self.send_json({
                "board": board_idx,
                "action": "all_on" if state else "all_off",
                "state": "on" if state else "off",
                "count": board["size"],
            })

        elif path == "/api/boards":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            # Add a new board
            addr = body.get("address", "0x21")
            bus = body.get("bus", 2)
            size = body.get("size", 16)
            name = body.get("name", f"Board")
            with config_lock:
                config = load_config()
                # Check for duplicate address on same bus
                for b in config["boards"]:
                    if b["address"] == addr and b["bus"] == bus:
                        self.send_json({"error": f"Board at {addr} on bus {bus} already exists"}, 400)
                        return
                new_board = {
                    "address": addr,
                    "bus": bus,
                    "size": size,
                    "name": name,
                    "relays": {str(i): f"Relay {i}" for i in range(size)},
                }
                config["boards"].append(new_board)
                save_config(config)
            # Initialize the new board
            try:
                i2c_init_board(bus, addr)
            except Exception:
                pass
            self.send_json({"ok": True})

        elif path == "/api/gpios":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            # Add a new GPIO
            try:
                wpi = int(body.get("wpi"))
            except (TypeError, ValueError):
                self.send_json({"error": "wpi (integer wiringPi number) is required"}, 400)
                return
            name = body.get("name") or f"GPIO {wpi}"
            pin_val = body.get("pin")
            pin = None
            if pin_val is not None and pin_val != "":
                try:
                    pin = int(pin_val)
                except (TypeError, ValueError):
                    self.send_json({"error": "pin must be an integer"}, 400)
                    return
            try:
                initial = parse_state(body.get("state", False))
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            with config_lock:
                config = load_config()
                for g in config["gpios"]:
                    if g["wpi"] == wpi:
                        self.send_json({"error": f"GPIO wPi {wpi} already configured"}, 400)
                        return
                config["gpios"].append({
                    "wpi": wpi,
                    "pin": pin,
                    "name": name,
                    "state": initial,
                })
                save_config(config)
            try:
                gpio_init_pin(wpi, initial)
            except Exception as e:
                self.send_json({"error": f"gpio init failed: {e}"}, 500)
                return
            self.send_json({"ok": True})

        elif path == "/api/gpio/toggle":
            try:
                wpi = int(body.get("wpi"))
            except (TypeError, ValueError):
                self.send_json({"error": "wpi is required"}, 400)
                return
            if not self.authorize_gpio(wpi):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                entry = next((g for g in config["gpios"] if g["wpi"] == wpi), None)
                if entry is None:
                    self.send_json({"error": f"GPIO wPi {wpi} not configured"}, 400)
                    return
            try:
                new_state = gpio_toggle_pin(wpi)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            with config_lock:
                config = load_config()
                for g in config["gpios"]:
                    if g["wpi"] == wpi:
                        g["state"] = new_state
                        break
                save_config(config)
            self.send_json({
                "wpi": wpi,
                "name": entry.get("name", f"GPIO {wpi}"),
                "state": "on" if new_state else "off",
            })

        elif path == "/api/gpio/set":
            try:
                wpi = int(body.get("wpi"))
            except (TypeError, ValueError):
                self.send_json({"error": "wpi is required"}, 400)
                return
            try:
                state = parse_state(body.get("state", False))
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            if not self.authorize_gpio(wpi):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                entry = next((g for g in config["gpios"] if g["wpi"] == wpi), None)
                if entry is None:
                    self.send_json({"error": f"GPIO wPi {wpi} not configured"}, 400)
                    return
            try:
                gpio_write(wpi, state)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            with config_lock:
                config = load_config()
                for g in config["gpios"]:
                    if g["wpi"] == wpi:
                        g["state"] = state
                        break
                save_config(config)
            self.send_json({
                "wpi": wpi,
                "name": entry.get("name", f"GPIO {wpi}"),
                "state": "on" if state else "off",
            })

        elif path == "/api/pwm/set":
            chip_idx = body.get("chip", 0)
            ch = body.get("channel")
            level = resolve_level(body)
            if level is None:
                self.send_json({"error": "level (0-4095) or percent (0-100) is required"}, 400)
                return
            config = load_config()
            chips = config.get("pwm", [])
            if not isinstance(ch, int) or chip_idx >= len(chips):
                self.send_json({"error": "Invalid chip or channel"}, 400)
                return
            if not self.authorize_pwm_channel(chip_idx, ch):
                self.forbidden()
                return
            chip = chips[chip_idx]
            if ch not in pca_enabled_channels(chip):
                self.send_json({"error": f"Channel {ch} is not configured on this chip"}, 400)
                return
            name, phase = pca_channel_cfg(chip, ch)
            try:
                written = pca_set_channel(chip["bus"], chip["address"], ch, level, phase)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({
                "chip": chip_idx,
                "channel": ch,
                "name": name,
                "level": written,
                "percent": round(100.0 * written / PWM_MAX, 2),
            })

        elif path == "/api/pwm/all":
            chip_idx = body.get("chip", 0)
            level = resolve_level(body)
            if level is None:
                self.send_json({"error": "level (0-4095) or percent (0-100) is required"}, 400)
                return
            config = load_config()
            chips = config.get("pwm", [])
            if chip_idx >= len(chips):
                self.send_json({"error": "Invalid chip"}, 400)
                return
            chip = chips[chip_idx]
            enabled = pca_enabled_channels(chip)
            if not self.authorize_pwm_channels(chip_idx, enabled):
                self.forbidden()
                return
            try:
                if level == 0:
                    pca_all_off(chip["bus"], chip["address"])
                else:
                    # per-channel so each keeps its own stagger phase
                    for ch in enabled:
                        _, phase = pca_channel_cfg(chip, ch)
                        pca_set_channel(chip["bus"], chip["address"], ch, level, phase)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({
                "chip": chip_idx,
                "level": level,
                "percent": round(100.0 * level / PWM_MAX, 2),
                "count": len(enabled),
            })

        elif path == "/api/pwm/off":
            config = load_config()
            chips = config.get("pwm", [])
            chip_idx = body.get("chip")
            targets = list(range(len(chips))) if chip_idx is None else [chip_idx]
            if not all(self.authorize_pwm_channels(i, pca_enabled_channels(chips[i]))
                       for i in targets if i < len(chips)):
                self.forbidden()
                return
            errors = []
            for i in targets:
                if i >= len(chips):
                    errors.append(f"invalid chip {i}")
                    continue
                try:
                    pca_all_off(chips[i]["bus"], chips[i]["address"])
                except Exception as e:
                    errors.append(str(e))
            if errors:
                self.send_json({"error": "; ".join(errors)}, 500)
                return
            self.send_json({"ok": True, "chips": len(targets)})

        elif path == "/api/pwm/group":
            chip_idx = body.get("chip", 0)
            gi = body.get("group", 0)
            level = resolve_level(body)
            if level is None:
                self.send_json({"error": "level (0-4095) or percent (0-100) is required"}, 400)
                return
            config = load_config()
            chips = config.get("pwm", [])
            if chip_idx >= len(chips):
                self.send_json({"error": "Invalid chip"}, 400)
                return
            chip = chips[chip_idx]
            if not isinstance(gi, int) or gi >= len(chip.get("groups", [])):
                self.send_json({"error": "Invalid group"}, 400)
                return
            gname, gch, gweights = pca_group_cfg(chip, gi)
            if not self.authorize_pwm_channels(chip_idx, gch):
                self.forbidden()
                return
            targets = group_levels_for(gch, gweights, level)
            try:
                for ch, lvl in targets.items():
                    _, phase = pca_channel_cfg(chip, ch)
                    pca_set_channel(chip["bus"], chip["address"], ch, lvl, phase)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({
                "chip": chip_idx,
                "group": gi,
                "name": gname,
                "level": level,
                "percent": round(100.0 * level / PWM_MAX, 2),
                "channels": {str(k): v for k, v in targets.items()},
            })

        elif path == "/api/pwm/groups":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            chip_idx = body.get("chip", 0)
            name = body.get("name") or "Group"
            channels = body.get("channels")
            if not isinstance(channels, list) or not channels:
                self.send_json({"error": "channels must be a non-empty list"}, 400)
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                enabled = pca_enabled_channels(chip)
                bad = [c for c in channels if not isinstance(c, int) or c not in enabled]
                if bad:
                    self.send_json({"error": f"channels not configured on this chip: {bad}"}, 400)
                    return
                chip.setdefault("groups", []).append({
                    "name": name,
                    "channels": channels,
                })
                save_config(config)
            self.send_json({"ok": True, "group": len(chip["groups"]) - 1})

        elif path == "/api/pwm/channels":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            chip_idx = body.get("chip", 0)
            try:
                ch = int(body.get("channel"))
            except (TypeError, ValueError):
                self.send_json({"error": "channel is required"}, 400)
                return
            if not 0 <= ch < PCA_CHANNELS:
                self.send_json({"error": f"channel must be 0-{PCA_CHANNELS - 1}"}, 400)
                return
            name = body.get("name") or f"Ch {ch}"
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                if ch in pca_enabled_channels(chip):
                    self.send_json({"error": f"Channel {ch} is already configured"}, 400)
                    return
                chans = pca_materialize_channels(chip)
                entry = {"name": name}
                if body.get("phase") is not None:
                    try:
                        entry["phase"] = int(body["phase"]) & 0x0FFF
                    except (TypeError, ValueError):
                        self.send_json({"error": "phase must be an integer"}, 400)
                        return
                chans[str(ch)] = entry
                save_config(config)
            # a channel that just came under management must start dark
            try:
                _, phase = pca_channel_cfg(chip, ch)
                pca_set_channel(chip["bus"], chip["address"], ch, 0, phase)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({"ok": True, "channel": ch, "name": name})

        elif path == "/api/pwm/reset":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            chip_idx = body.get("chip", 0)
            config = load_config()
            chips = config.get("pwm", [])
            if chip_idx >= len(chips):
                self.send_json({"error": "Invalid chip"}, 400)
                return
            chip = chips[chip_idx]
            try:
                pca_init(chip["bus"], chip["address"],
                         chip.get("freq_hz", PWM_DEFAULT_FREQ))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({"ok": True})

        elif path == "/api/pwm":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            addr = body.get("address", "0x40")
            bus = body.get("bus", 2)
            size = body.get("size", 16)
            name = body.get("name", "LED PWM")
            freq = body.get("freq_hz", PWM_DEFAULT_FREQ)
            size = max(1, min(PCA_CHANNELS, int(size)))
            with config_lock:
                config = load_config()
                clash = i2c_address_conflict(config, bus, addr)
                if clash:
                    self.send_json({"error": clash}, 400)
                    return
                config["pwm"].append({
                    "address": addr,
                    "bus": bus,
                    "name": name,
                    "freq_hz": freq,
                    "channels": {str(i): {"name": f"Ch {i}"} for i in range(size)},
                })
                save_config(config)
            try:
                pca_init(bus, addr, freq)
            except Exception as e:
                self.send_json({"error": f"pwm init failed: {e}"}, 500)
                return
            self.send_json({"ok": True})

        elif path == "/api/board/reset":
            board_idx = body.get("board", 0)
            config = load_config()
            if board_idx >= len(config["boards"]):
                self.send_json({"error": "Invalid board"}, 400)
                return
            board = config["boards"][board_idx]
            if not self.authorize_board_bulk(board_idx, board["size"]):
                self.forbidden()
                return
            try:
                i2c_init_board(board["bus"], board["address"])
                set_all(board["bus"], board["address"], board["size"], False)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self.read_body()

        if path == "/api/relay/name":
            board_idx = body.get("board", 0)
            relay_num = body.get("relay", 0)
            name = body.get("name", "")
            if not self.authorize_relay(board_idx, relay_num):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                if board_idx >= len(config["boards"]):
                    self.send_json({"error": "Invalid board"}, 400)
                    return
                board = config["boards"][board_idx]
                if "relays" not in board:
                    board["relays"] = {}
                board["relays"][str(relay_num)] = name
                save_config(config)
            self.send_json({"ok": True})

        elif path == "/api/board/name":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            board_idx = body.get("board", 0)
            name = body.get("name", "")
            with config_lock:
                config = load_config()
                if board_idx >= len(config["boards"]):
                    self.send_json({"error": "Invalid board"}, 400)
                    return
                config["boards"][board_idx]["name"] = name
                save_config(config)
            self.send_json({"ok": True})

        elif path == "/api/gpio/name":
            try:
                wpi = int(body.get("wpi"))
            except (TypeError, ValueError):
                self.send_json({"error": "wpi is required"}, 400)
                return
            name = body.get("name", "")
            if not self.authorize_gpio(wpi):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                entry = next((g for g in config["gpios"] if g["wpi"] == wpi), None)
                if entry is None:
                    self.send_json({"error": f"GPIO wPi {wpi} not configured"}, 400)
                    return
                entry["name"] = name
                save_config(config)
            self.send_json({"ok": True})

        elif path == "/api/gpio/pin":
            try:
                wpi = int(body.get("wpi"))
            except (TypeError, ValueError):
                self.send_json({"error": "wpi is required"}, 400)
                return
            pin_val = body.get("pin")
            pin = None
            if pin_val is not None and pin_val != "":
                try:
                    pin = int(pin_val)
                except (TypeError, ValueError):
                    self.send_json({"error": "pin must be an integer"}, 400)
                    return
            if not self.authorize_gpio(wpi):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                entry = next((g for g in config["gpios"] if g["wpi"] == wpi), None)
                if entry is None:
                    self.send_json({"error": f"GPIO wPi {wpi} not configured"}, 400)
                    return
                entry["pin"] = pin
                save_config(config)
            self.send_json({"ok": True})

        elif path == "/api/pwm/name":
            chip_idx = body.get("chip", 0)
            ch = body.get("channel")
            name = body.get("name", "")
            if ch is None:
                if not self.authorize_owner_only():
                    self.forbidden()
                    return
            elif not self.authorize_pwm_channel(chip_idx, ch):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                if ch is None:
                    chip["name"] = name
                else:
                    entry = chip.setdefault("channels", {}).get(str(ch))
                    if isinstance(entry, str) or entry is None:
                        entry = {}
                    entry["name"] = name
                    chip["channels"][str(ch)] = entry
                save_config(config)
            self.send_json({"ok": True})

        elif path == "/api/pwm/group":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            chip_idx = body.get("chip", 0)
            gi = body.get("group", 0)
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                if not isinstance(gi, int) or gi >= len(chip.get("groups", [])):
                    self.send_json({"error": "Invalid group"}, 400)
                    return
                group = chip["groups"][gi]
                enabled = pca_enabled_channels(chip)
                if "name" in body:
                    group["name"] = body["name"]
                if "channels" in body:
                    chans = body["channels"]
                    if not isinstance(chans, list) or not chans:
                        self.send_json({"error": "channels must be a non-empty list"}, 400)
                        return
                    bad = [c for c in chans if not isinstance(c, int) or c not in enabled]
                    if bad:
                        self.send_json({"error": f"channels not configured on this chip: {bad}"}, 400)
                        return
                    group["channels"] = chans
                if "weights" in body:
                    weights = body["weights"]
                    if not isinstance(weights, dict):
                        self.send_json({"error": "weights must be an object keyed by channel"}, 400)
                        return
                    try:
                        group["weights"] = {
                            str(int(k)): max(0, min(PWM_MAX, int(v)))
                            for k, v in weights.items()
                        }
                    except (TypeError, ValueError):
                        self.send_json({"error": "weights must be integers 0-4095"}, 400)
                        return
                save_config(config)
            name, chans, w = pca_group_cfg(chip, gi)
            self.send_json({
                "ok": True,
                "name": name,
                "channels": chans,
                "weights": {str(k): v for k, v in w.items()},
            })

        elif path == "/api/pwm/settings":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            chip_idx = body.get("chip", 0)
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                old_addr, old_bus = chip["address"], chip["bus"]
                new_addr = body.get("address", old_addr)
                new_bus = body.get("bus", old_bus)
                try:
                    new_bus = int(new_bus)
                except (TypeError, ValueError):
                    self.send_json({"error": "bus must be an integer"}, 400)
                    return
                clash = i2c_address_conflict(config, new_bus, new_addr, skip_pwm=chip_idx)
                if clash:
                    self.send_json({"error": clash}, 400)
                    return
                chip["address"] = new_addr
                chip["bus"] = new_bus
                save_config(config)
            moved = (new_addr, new_bus) != (old_addr, old_bus)
            if moved:
                # blank the old address first, or a strip stays lit on a chip
                # nothing manages any more
                try:
                    pca_all_off(old_bus, old_addr)
                except Exception:
                    pass
            try:
                pca_init(new_bus, new_addr, chip.get("freq_hz", PWM_DEFAULT_FREQ))
            except Exception as e:
                self.send_json({"error": f"no response at {new_addr} on bus {new_bus}: {e}"}, 500)
                return
            self.send_json({"ok": True, "address": new_addr, "bus": new_bus, "moved": moved})

        elif path == "/api/pwm/freq":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            chip_idx = body.get("chip", 0)
            try:
                freq = float(body.get("freq_hz"))
            except (TypeError, ValueError):
                self.send_json({"error": "freq_hz is required"}, 400)
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                chip["freq_hz"] = freq
                save_config(config)
            try:
                ps, achieved = pca_set_freq(chip["bus"], chip["address"], freq)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            # PRE_SCALE is an integer divider, so the achieved frequency is
            # quantised and drifts from the request. Report the truth.
            self.send_json({
                "chip": chip_idx,
                "requested_hz": freq,
                "prescale": ps,
                "freq_hz": round(achieved, 1),
            })

        elif path == "/api/pwm/phase":
            chip_idx = body.get("chip", 0)
            ch = body.get("channel")
            try:
                phase = int(body.get("phase")) & 0x0FFF
            except (TypeError, ValueError):
                self.send_json({"error": "phase (0-4095) is required"}, 400)
                return
            if not self.authorize_pwm_channel(chip_idx, ch):
                self.forbidden()
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])) or ch is None:
                    self.send_json({"error": "Invalid chip or channel"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                entry = chip.setdefault("channels", {}).get(str(ch))
                if isinstance(entry, str):
                    entry = {"name": entry}
                elif entry is None:
                    entry = {}
                entry["phase"] = phase
                chip["channels"][str(ch)] = entry
                save_config(config)
            # re-apply at the current duty so the new phase takes effect now
            try:
                regs = pca_dump(chip["bus"], chip["address"])
                if regs:
                    base = PCA_LED0_BASE + 4 * ch
                    pca_set_channel(chip["bus"], chip["address"], ch,
                                    pca_level(regs[base:base + 4]), phase)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({"ok": True, "phase": phase})

        elif path == "/api/board/layout":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            board_idx = body.get("board", 0)
            layout = body.get("layout", "grid")
            with config_lock:
                config = load_config()
                if board_idx >= len(config["boards"]):
                    self.send_json({"error": "Invalid board"}, 400)
                    return
                config["boards"][board_idx]["layout"] = layout
                save_config(config)
            self.send_json({"ok": True})

        elif path == "/api/board/flip":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            board_idx = body.get("board", 0)
            with config_lock:
                config = load_config()
                if board_idx >= len(config["boards"]):
                    self.send_json({"error": "Invalid board"}, 400)
                    return
                config["boards"][board_idx]["flipped"] = not config["boards"][board_idx].get("flipped", False)
                save_config(config)
            self.send_json({"flipped": config["boards"][board_idx]["flipped"]})

        elif path == "/api/board/settings":
            if not self.authorize_owner_only():
                self.forbidden()
                return
            board_idx = body.get("board", 0)
            with config_lock:
                config = load_config()
                if board_idx >= len(config["boards"]):
                    self.send_json({"error": "Invalid board"}, 400)
                    return
                board = config["boards"][board_idx]
                if "address" in body:
                    new_addr = body["address"]
                    new_bus = body.get("bus", board["bus"])
                    for i, b in enumerate(config["boards"]):
                        if i != board_idx and b["address"] == new_addr and b["bus"] == new_bus:
                            self.send_json({"error": f"Board at {new_addr} on bus {new_bus} already exists"}, 400)
                            return
                    board["address"] = new_addr
                if "bus" in body:
                    board["bus"] = body["bus"]
                if "size" in body:
                    old_size = board["size"]
                    new_size = body["size"]
                    board["size"] = new_size
                    if new_size > old_size:
                        for i in range(old_size, new_size):
                            if str(i) not in board.get("relays", {}):
                                board.setdefault("relays", {})[str(i)] = f"Relay {i}"
                save_config(config)
            try:
                addr = config["boards"][board_idx]["address"]
                bus = config["boards"][board_idx]["bus"]
                i2c_init_board(bus, addr)
            except Exception:
                pass
            self.send_json({"ok": True})

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not self.authorize_owner_only():
            self.forbidden()
            return

        if path.startswith("/api/boards/"):
            try:
                board_idx = int(path.split("/")[-1])
            except ValueError:
                self.send_json({"error": "Invalid board index"}, 400)
                return
            with config_lock:
                config = load_config()
                if board_idx >= len(config["boards"]):
                    self.send_json({"error": "Invalid board"}, 400)
                    return
                config["boards"].pop(board_idx)
                save_config(config)
            self.send_json({"ok": True})
        elif path.startswith("/api/pwm/channels/"):
            # tested before the bare /api/pwm/<idx> case, which would otherwise
            # read the channel number as a chip index
            parts = path.split("/")
            try:
                chip_idx, ch = int(parts[-2]), int(parts[-1])
            except (ValueError, IndexError):
                self.send_json({"error": "expected /api/pwm/channels/<chip>/<channel>"}, 400)
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                if ch not in pca_enabled_channels(chip):
                    self.send_json({"error": f"Channel {ch} is not configured"}, 400)
                    return
                chans = pca_materialize_channels(chip)
                chans.pop(str(ch), None)
                # a group must never point at a channel the chip no longer
                # manages; one left with no members goes too
                groups = chip.get("groups", [])
                for g in groups:
                    g["channels"] = [c for c in g.get("channels", []) if c != ch]
                    if isinstance(g.get("weights"), dict):
                        g["weights"].pop(str(ch), None)
                dropped = [g.get("name") for g in groups if not g.get("channels")]
                chip["groups"] = [g for g in groups if g.get("channels")]
                save_config(config)
            # never leave a lit channel behind that nothing manages
            try:
                pca_set_channel(chip["bus"], chip["address"], ch, 0, 0)
            except Exception:
                pass
            self.send_json({"ok": True, "channel": ch, "groups_dropped": dropped})
        elif path.startswith("/api/pwm/groups/"):
            # must be tested before the bare /api/pwm/<idx> case below, which
            # would otherwise read the group index as a chip index
            parts = path.split("/")
            try:
                chip_idx, gi = int(parts[-2]), int(parts[-1])
            except (ValueError, IndexError):
                self.send_json({"error": "expected /api/pwm/groups/<chip>/<group>"}, 400)
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"][chip_idx]
                if gi >= len(chip.get("groups", [])):
                    self.send_json({"error": "Invalid group"}, 400)
                    return
                chip["groups"].pop(gi)
                save_config(config)
            self.send_json({"ok": True})
        elif path.startswith("/api/pwm/"):
            try:
                chip_idx = int(path.split("/")[-1])
            except ValueError:
                self.send_json({"error": "Invalid chip index"}, 400)
                return
            with config_lock:
                config = load_config()
                if chip_idx >= len(config.get("pwm", [])):
                    self.send_json({"error": "Invalid chip"}, 400)
                    return
                chip = config["pwm"].pop(chip_idx)
                save_config(config)
            # leaving a strip lit on a chip nobody is managing any more is the
            # one outcome worth avoiding here
            try:
                pca_all_off(chip["bus"], chip["address"])
            except Exception:
                pass
            self.send_json({"ok": True})
        elif path.startswith("/api/gpios/"):
            try:
                wpi = int(path.split("/")[-1])
            except ValueError:
                self.send_json({"error": "Invalid wpi"}, 400)
                return
            with config_lock:
                config = load_config()
                before = len(config["gpios"])
                config["gpios"] = [g for g in config["gpios"] if g["wpi"] != wpi]
                if len(config["gpios"]) == before:
                    self.send_json({"error": f"GPIO wPi {wpi} not configured"}, 400)
                    return
                save_config(config)
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "Not found"}, 404)


def init_boards(config):
    """Initialize all configured boards and set all relays OFF."""
    for board in config["boards"]:
        try:
            # i2c_init_board already leaves every relay off, as part of coming
            # up in an order that never drives them. A set_all afterwards would
            # be redundant, and before it would be the very pulse being avoided.
            i2c_init_board(board["bus"], board["address"])
            print(f"  Initialized board at {board['address']} on bus {board['bus']} - all OFF")
        except Exception as e:
            print(f"  Failed to init board at {board['address']}: {e}")


def init_gpios(config):
    """Restore each configured GPIO to its last known state."""
    for g in config.get("gpios", []):
        wpi = g["wpi"]
        state = bool(g.get("state", False))
        try:
            gpio_init_pin(wpi, state)
            print(f"  Initialized GPIO wPi {wpi} ({g.get('name', '?')}) -> {'ON' if state else 'OFF'}")
        except Exception as e:
            print(f"  Failed to init GPIO wPi {wpi}: {e}")


def init_pwm(config):
    """Initialize each PWM chip and leave every channel dark.

    Same contract as init_boards: a restart is a known-safe state, not a
    resume. Nothing persists a duty across a restart on purpose, so a stale
    config can never bring a strip back up at an unexpected level.
    """
    for chip in config.get("pwm", []):
        try:
            if pca_dump(chip["bus"], chip["address"]) is None:
                # Its power runs through a relay init_boards just turned off,
                # so silence here is expected rather than a failure to report.
                print(f"  PWM at {chip['address']} on bus {chip['bus']} is not "
                      f"answering (unpowered?) - will be set up when it appears")
                continue
            pca_init(chip["bus"], chip["address"],
                     chip.get("freq_hz", PWM_DEFAULT_FREQ))
            regs = pca_dump(chip["bus"], chip["address"])
            hz = pca_freq_for(regs[PCA_PRESCALE]) if regs else 0
            print(f"  Initialized PWM at {chip['address']} on bus {chip['bus']} "
                  f"- {hz:.1f} Hz, all channels OFF")
        except Exception as e:
            print(f"  Failed to init PWM at {chip['address']}: {e}")


class DualStackHTTPServer(ThreadingHTTPServer):
    """HTTP server on a dual-stack socket.

    IPV6_V6ONLY is cleared so a single socket serves both families: IPv4
    callers arrive as v4-mapped addresses, so existing http://<v4>:8080
    users are unaffected.
    """

    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        ThreadingHTTPServer.server_bind(self)


def main():
    config = load_config()

    print("Initializing boards...")
    init_boards(config)
    print("Initializing GPIOs...")
    init_gpios(config)
    print("Initializing PWM...")
    init_pwm(config)

    print("Starting bench sampler...")
    sampler.fresh()          # serve a real reading from the first request on
    sampler.start()

    # An explicitly IPv4 RELAY_PANEL_HOST still gets a plain v4 socket.
    server_cls = DualStackHTTPServer if ":" in LISTEN_HOST else ThreadingHTTPServer
    server = server_cls((LISTEN_HOST, LISTEN_PORT), RelayHandler)
    shown = f"[{LISTEN_HOST}]" if ":" in LISTEN_HOST else LISTEN_HOST
    print(f"Relay Control Panel running on http://{shown}:{LISTEN_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
