#!/usr/bin/env python3
"""Relay Control Panel - Web interface for XL9535 I2C relay boards."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path("/opt/relay-panel")
CONFIG_FILE = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

USB_SYS_PATH = Path("/sys/bus/usb/devices")
USB_VID_FILTER = "a108"  # Ingenic bootrom (c309) / U-Boot DFU gadget (4d44)

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
    """Read current output register states. Returns (port_a, port_b) as ints."""
    with i2c_lock:
        out_a, rc_a = i2c_cmd("i2cget", bus, addr, "0x02")
        out_b, rc_b = i2c_cmd("i2cget", bus, addr, "0x03")
    port_a = int(out_a, 16) if rc_a == 0 and out_a.startswith("0x") else 0
    port_b = int(out_b, 16) if rc_b == 0 and out_b.startswith("0x") else 0
    return port_a, port_b


def i2c_write_reg(bus, addr, reg, value):
    """Write a value to a register."""
    with i2c_lock:
        i2c_cmd("i2cset", bus, addr, reg, hex(value))


def get_relay_state(bus, addr, relay_num):
    """Get the state of a single relay."""
    port_a, port_b = i2c_read_state(bus, addr)
    if relay_num < 8:
        return bool(port_a & (1 << relay_num))
    else:
        return bool(port_b & (1 << (relay_num - 8)))


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
                except ValueError:
                    pass                          # "XX" = unreadable register
    return regs


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
        base = mode1 & ~PCA_M1_RESTART & 0xFF
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1), hex(base | PCA_M1_SLEEP))
        i2c_cmd("i2cset", bus, addr, hex(PCA_PRESCALE), hex(ps))
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1), hex(base & ~PCA_M1_SLEEP))
        time.sleep(0.001)                        # datasheet wants 500 us
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1),
                hex((base & ~PCA_M1_SLEEP) | PCA_M1_RESTART))
    return ps, pca_freq_for(ps)


def pca_init(bus, addr, freq_hz):
    """Wake the chip, enable auto-increment, set frequency, all channels off."""
    with i2c_lock:
        # totem-pole non-inverting: output high turns a MOSFET module's gate on
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE2), "0x04")
        i2c_cmd("i2cset", bus, addr, hex(PCA_MODE1), hex(PCA_M1_AI))
    pca_all_off(bus, addr)
    # RESTART at the end of pca_set_freq resumes the pre-sleep duty, so the
    # all-off above is what makes the restart land on "dark" rather than on
    # whatever was lit before.
    pca_set_freq(bus, addr, freq_hz)


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


def get_pwm_state(config):
    """Live state of every configured PWM chip, read back from hardware."""
    result = []
    for idx, chip in enumerate(config.get("pwm", [])):
        bus = chip["bus"]
        addr = chip["address"]
        size = chip.get("size", 16)
        regs = pca_dump(bus, addr)
        prescale = regs[PCA_PRESCALE] if regs else 0
        channels = []
        for ch in range(size):
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
        result.append({
            "index": idx,
            "name": chip.get("name", f"PWM {idx}"),
            "address": addr,
            "bus": bus,
            "size": size,
            "prescale": prescale,
            "freq_hz": round(pca_freq_for(prescale), 1) if regs else 0,
            "channels": channels,
            "error": None if regs else f"no response at {addr} on bus {bus}",
        })
    return result


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
            self.send_json({"usb": get_usb_devices()})
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
            config = load_config()
            if board_idx >= len(config["boards"]):
                self.send_json({"error": "Invalid board"}, 400)
                return
            board = config["boards"][board_idx]
            new_state = toggle_relay(board["bus"], board["address"], relay_num)
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
            config = load_config()
            if board_idx >= len(config["boards"]):
                self.send_json({"error": "Invalid board"}, 400)
                return
            board = config["boards"][board_idx]
            set_relay(board["bus"], board["address"], relay_num, state)
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
            set_all(board["bus"], board["address"], board["size"], state)
            self.send_json({
                "board": board_idx,
                "action": "all_on" if state else "all_off",
                "state": "on" if state else "off",
                "count": board["size"],
            })

        elif path == "/api/boards":
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
            chip = chips[chip_idx]
            if not 0 <= ch < chip.get("size", 16):
                self.send_json({"error": "Channel out of range"}, 400)
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
            try:
                if level == 0:
                    pca_all_off(chip["bus"], chip["address"])
                else:
                    # per-channel so each keeps its own stagger phase
                    for ch in range(chip.get("size", 16)):
                        _, phase = pca_channel_cfg(chip, ch)
                        pca_set_channel(chip["bus"], chip["address"], ch, level, phase)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({
                "chip": chip_idx,
                "level": level,
                "percent": round(100.0 * level / PWM_MAX, 2),
                "count": chip.get("size", 16),
            })

        elif path == "/api/pwm/off":
            config = load_config()
            chips = config.get("pwm", [])
            chip_idx = body.get("chip")
            targets = list(range(len(chips))) if chip_idx is None else [chip_idx]
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

        elif path == "/api/pwm/reset":
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
            addr = body.get("address", "0x40")
            bus = body.get("bus", 2)
            size = body.get("size", 16)
            name = body.get("name", "LED PWM")
            freq = body.get("freq_hz", PWM_DEFAULT_FREQ)
            with config_lock:
                config = load_config()
                for c in config["pwm"]:
                    if c["address"] == addr and c["bus"] == bus:
                        self.send_json({"error": f"PWM chip at {addr} on bus {bus} already exists"}, 400)
                        return
                config["pwm"].append({
                    "address": addr,
                    "bus": bus,
                    "size": size,
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

        elif path == "/api/pwm/freq":
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

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), RelayHandler)
    print(f"Relay Control Panel running on http://{LISTEN_HOST}:{LISTEN_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
