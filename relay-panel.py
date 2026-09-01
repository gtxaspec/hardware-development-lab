#!/usr/bin/env python3
"""Relay Control Panel - Web interface for XL9535 I2C relay boards."""

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(os.environ.get("RELAY_PANEL_DIR", "/opt/relay-panel"))
CONFIG_FILE = BASE_DIR / "config.json"
TOKENS_FILE = BASE_DIR / "tokens.json"
STATIC_DIR = BASE_DIR / "static"
LISTEN_HOST = os.environ.get("RELAY_PANEL_HOST", "::")
LISTEN_PORT = int(os.environ.get("RELAY_PANEL_PORT", "8080"))

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

i2c_lock = threading.Lock()
gpio_lock = threading.Lock()
config_lock = threading.Lock()


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


def i2c_cmd(cmd, bus, addr, *args):
    """Run an i2c command and return output."""
    full_cmd = [cmd, "-y", str(bus), str(addr)] + [str(a) for a in args]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1


def i2c_init_board(bus, addr):
    """Set all pins as output."""
    with i2c_lock:
        i2c_cmd("i2cset", bus, addr, "0x06", "0x00")
        i2c_cmd("i2cset", bus, addr, "0x07", "0x00")


def i2c_read_state(bus, addr):
    """Read current output register states. Returns (port_a, port_b) as ints.

    Raises on a failed read rather than defaulting to zero. A wedged bus would
    otherwise be indistinguishable from every relay being off, and on a bench
    where relays feed camera power that is the last thing to report
    confidently. get_full_state turns the exception into the board's `error`.
    """
    with i2c_lock:
        out_a, rc_a = i2c_cmd("i2cget", bus, addr, "0x02")
        out_b, rc_b = i2c_cmd("i2cget", bus, addr, "0x03")
    ok_a = rc_a == 0 and out_a.startswith("0x")
    ok_b = rc_b == 0 and out_b.startswith("0x")
    if not (ok_a and ok_b):
        raise RuntimeError(f"no response at {addr} on bus {bus}")
    return int(out_a, 16), int(out_b, 16)


def i2c_write_reg(bus, addr, reg, value):
    """Write a value to a register."""
    with i2c_lock:
        i2c_cmd("i2cset", bus, addr, reg, hex(value))


def set_relay(bus, addr, relay_num, state):
    """Set a relay on or off."""
    port_a, port_b = i2c_read_state(bus, addr)
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
    port_a, port_b = i2c_read_state(bus, addr)
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
    """Set all relays on a board on or off."""
    if size <= 8:
        mask_a = (1 << size) - 1 if state else 0
        i2c_write_reg(bus, addr, "0x02", mask_a)
    else:
        mask_a = 0xFF if state else 0
        mask_b = ((1 << (size - 8)) - 1) if state else 0
        i2c_write_reg(bus, addr, "0x02", mask_a)
        i2c_write_reg(bus, addr, "0x03", mask_b)


def gpio_cmd(*args):
    """Run a `gpio` CLI command and return (stdout, returncode)."""
    full_cmd = ["gpio"] + [str(a) for a in args]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return str(e), 1


def gpio_init_pin(wpi, initial_state=False):
    """Set a pin to output mode and to the given initial state."""
    with gpio_lock:
        gpio_cmd("mode", wpi, "out")
        gpio_cmd("write", wpi, 1 if initial_state else 0)


def gpio_read(wpi):
    """Read current state of a pin (True/False)."""
    with gpio_lock:
        out, rc = gpio_cmd("read", wpi)
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


def get_gpio_states(gpios):
    """Return list of GPIO entries with live state read from hardware."""
    result = []
    for g in gpios:
        wpi = g["wpi"]
        try:
            state = gpio_read(wpi)
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
PCA_M1_AI = 0x20          # auto-increment, required for the 4-byte block write
PCA_M1_SLEEP = 0x10

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


def pca_dump(bus, addr):
    """Read all 256 registers in a single i2cdump. Returns a list or None.

    One subprocess per poll instead of 64 i2cget calls for a 16-channel read.
    """
    with i2c_lock:
        out, rc = i2c_cmd("i2cdump", bus, addr, "b")
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
        time.sleep(0.001)                        # datasheet wants 500 us
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
    with i2c_lock:
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1),
                hex((keep | PCA_M1_AI) & ~PCA_M1_SLEEP & ~PCA_M1_RESTART & 0xFF))
        cur, rc = i2c_cmd("i2cget", bus, addr, hex(PCA_MODE1))
    if rc != 0 or not cur.startswith("0x"):
        raise RuntimeError(f"MODE1 readback failed at {addr} on bus {bus}: {cur}")
    return int(cur, 16)


def pca_init(bus, addr, freq_hz):
    """Wake the chip, enable auto-increment, set frequency, all channels off."""
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


def get_pwm_state(config):
    """Live state of every configured PWM chip, read back from hardware."""
    result = []
    for idx, chip in enumerate(config.get("pwm", [])):
        bus = chip["bus"]
        addr = chip["address"]
        enabled = pca_enabled_channels(chip)
        regs = pca_dump(bus, addr)
        # The poll already holds MODE1, so it is the cheapest place to notice
        # that a glitch reset the chip and put auto-increment back before the
        # next write silently no-ops. Repairing state during a read is worth
        # it here: the alternative is a bench that lies about its lights.
        if regs and not regs[PCA_MODE1] & PCA_M1_AI:
            try:
                pca_set_mode1_ai(bus, addr, keep=regs[PCA_MODE1])
                print(f"  PWM {addr} on bus {bus}: re-enabled auto-increment "
                      f"(MODE1 was {regs[PCA_MODE1]:#04x})")
                regs = pca_dump(bus, addr) or regs
            except Exception as e:
                print(f"  PWM {addr} on bus {bus}: auto-increment repair failed: {e}")
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
            "error": None if regs else f"no response at {addr} on bus {bus}",
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


def get_board_states(board):
    """Get all relay states for a board."""
    bus = board["bus"]
    addr = board["address"]
    size = board["size"]
    port_a, port_b = i2c_read_state(bus, addr)
    states = {}
    for i in range(size):
        if i < 8:
            states[i] = bool(port_a & (1 << i))
        else:
            states[i] = bool(port_b & (1 << (i - 8)))
    return states


def get_full_state(config):
    """Get the full state of all boards and relays."""
    result = []
    for idx, board in enumerate(config["boards"]):
        try:
            states = get_board_states(board)
            error = None
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


class RelayHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the relay control panel."""

    # Drop a client that stalls mid-request instead of holding its thread
    # open indefinitely - a backgrounded mobile browser on a slow link will
    # otherwise sit in the middle of a request for as long as it likes.
    timeout = 30

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def send_json(self, data, status=200):
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/state":
            config = load_config()
            state = get_full_state(config)
            gpios = get_gpio_states(config.get("gpios", []))
            self.send_json({
                "boards": state,
                "gpios": gpios,
                "pwm": get_pwm_state(config),
                "usb": get_usb_devices(),
                # so the UI can label the section with the vendor actually
                # being watched rather than assuming the default
                "usb_vid": USB_VID_FILTER,
            })
            return

        if path == "/api/pwm":
            self.send_json({"pwm": get_pwm_state(load_config())})
            return

        if path == "/api/gpios":
            config = load_config()
            gpios = get_gpio_states(config.get("gpios", []))
            self.send_json({"gpios": gpios})
            return

        if path == "/api/usb":
            self.send_json({"usb": get_usb_devices(), "vid": USB_VID_FILTER})
            return

        # Serve static files
        if path == "" or path == "/":
            path = "/index.html"
        file_path = STATIC_DIR / path.lstrip("/")
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
            state = body.get("state", False)
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
            state = body.get("state", False)
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
            initial = bool(body.get("state", False))
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
            state = bool(body.get("state", False))
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
            i2c_init_board(board["bus"], board["address"])
            set_all(board["bus"], board["address"], board["size"], False)
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
    if not config["boards"]:
        print("No boards configured, creating default config...")
        config = {
            "boards": [{
                "address": "0x20",
                "bus": 2,
                "size": 16,
                "name": "Board 1",
                "relays": {str(i): f"Relay {i}" for i in range(16)},
            }]
        }
        save_config(config)

    print("Initializing boards...")
    init_boards(config)
    print("Initializing GPIOs...")
    init_gpios(config)
    print("Initializing PWM...")
    init_pwm(config)

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
