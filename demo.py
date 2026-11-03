#!/usr/bin/env python3
"""Run the panel with stubbed hardware, for demos and screenshots.

relay-panel.py reaches hardware only by shelling out to i2ctransfer, i2cget,
i2cset, i2cdump and gpio. This puts fakes for those tools on PATH ahead of the real
ones and points the server at a throwaway config, so the whole UI can be
driven on a machine with no I2C bus at all - and a bench with real hardware
is never touched.

    ./demo.py                 # serve on a free port, prints the URL
    ./demo.py --port 8099     # pick the port
    ./demo.py --screenshot docs/screenshot.png

The screenshot mode needs a Chromium binary; it looks on PATH and in
Playwright's cache, or takes --chrome.
"""
import argparse
import glob
import json
import os
import pathlib
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REPO = pathlib.Path(__file__).resolve().parent

# Deliberately generic: this config ends up in screenshots, so it should not
# describe anyone's actual bench.
DEMO_CONFIG = {
    "boards": [
        {"address": "0x20", "bus": 2, "size": 16, "name": "Bench Power",
         "layout": "board",
         "relays": {str(i): n for i, n in enumerate(
             ["Camera 1", "Camera 2", "Camera 3", "Camera 4", "Camera 5",
              "Camera 6", "Camera 7", "5V Rail", "Camera 8", "Camera 9",
              "Camera 10", "Camera 11", "Camera 12", "Camera 13", "Camera 14",
              "12V Rail"])}},
        {"address": "0x27", "bus": 2, "size": 16, "name": "Boot Select",
         "layout": "board",
         "relays": {str(i): f"Boot {i}" for i in range(16)}},
    ],
    "gpios": [
        {"wpi": 27, "pin": 40, "name": "USB Mux A", "state": True},
        {"wpi": 26, "pin": 38, "name": "USB Mux B", "state": False},
        {"wpi": 25, "pin": 37, "name": "Spare", "state": False},
    ],
    "pwm": [
        {"address": "0x40", "bus": 2, "name": "LED Strips", "freq_hz": 1526,
         "channels": {"0": {"name": "White Strip"}, "1": {"name": "RGB Red"},
                      "2": {"name": "RGB Green"}, "3": {"name": "RGB Blue"}},
         "groups": [{"name": "RGB Strip", "channels": [1, 2, 3]}]},
    ],
}

# Relays lit in the demo, as (board index, relay number).
DEMO_RELAYS = [(0, 0), (0, 3), (0, 7), (0, 15), (1, 2)]
DEMO_WHITE = 4095      # White Strip duty
DEMO_GROUP = 1024      # RGB group master

STUB = r'''#!/usr/bin/env python3
"""Fake i2ctransfer/i2cget/i2cset/i2cdump/gpio, dispatching on argv[0]."""
import json, os, pathlib, sys

TOOL = pathlib.Path(sys.argv[0]).name
STATE = pathlib.Path(os.environ["STUB_STATE"])
PWM_ADDR = os.environ.get("STUB_PWM_ADDR", "0x40")


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"i2c": {}, "gpio": {}, "pwm": {}}


def save(s):
    STATE.write_text(json.dumps(s))


def relay_read(s, addr, reg):
    regs = s.get("i2c", {}).get(addr, {})
    if reg in (0, 1):
        # Input pins follow the output latch when configured as outputs.
        output = regs.get(hex(reg + 2), 0xFF)
        direction = regs.get(hex(reg + 6), 0xFF)
        polarity = regs.get(hex(reg + 4), 0)
        return (output & ~direction) ^ polarity
    return regs.get(hex(reg), 0xFF if reg in (2, 3, 6, 7) else 0)


def pwm_page(chan):
    """Render the PCA9685 register page from the stub's channel state."""
    r = [0] * 256
    r[0x00], r[0x01], r[0xFE] = 0x20, 0x04, int(chan.get("prescale", 3))
    r[0x02], r[0x03], r[0x04], r[0x05] = 0xE2, 0xE4, 0xE8, 0xE0
    for ch in range(16):
        level = int(chan.get(str(ch), {}).get("level", 0))
        phase = int(chan.get(str(ch), {}).get("phase", 0))
        b = 0x06 + 4 * ch
        if level <= 0:
            r[b + 3] = 0x10                                   # FULL_OFF
        elif level >= 4095:
            r[b + 1] = 0x10                                   # FULL_ON
        else:
            on, off = phase & 0xFFF, (phase + level) & 0xFFF
            r[b], r[b + 1] = on & 0xFF, (on >> 8) & 0x0F
            r[b + 2], r[b + 3] = off & 0xFF, (off >> 8) & 0x0F
    head = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f    0123456789abcdef"
    rows = [f"{base:02x}: " + " ".join(f"{r[base + i]:02x}" for i in range(16))
            + "    " + "." * 16 for base in range(0, 256, 16)]
    return head + "\n" + "\n".join(rows)


s, a = load(), sys.argv[1:]

if TOOL == "i2cdump":
    print(pwm_page(s.setdefault("pwm", {})))

elif TOOL == "i2cget":
    print(f"0x{relay_read(s, a[2], int(a[3], 16)):02x}")

elif TOOL == "i2ctransfer":
    addr, first, count = a[2].split("@", 1)[1], int(a[3], 16), int(a[4][1:])
    print(" ".join(f"0x{relay_read(s, addr, first ^ (i % 2)):02x}"
                   for i in range(count)))

elif TOOL == "i2cset":
    addr, reg, vals = a[2], a[3], a[4:]
    if vals and vals[-1] == "i":
        vals = vals[:-1]
    if addr == PWM_ADDR:
        pwm = s.setdefault("pwm", {})
        if reg == "0xfe":
            pwm["prescale"] = int(vals[0], 16)
        elif int(reg, 16) == 0xFA and len(vals) >= 4:         # ALL_LED
            for ch in range(16):
                pwm.setdefault(str(ch), {})["level"] = 0
        elif 0x06 <= int(reg, 16) <= 0x45 and len(vals) >= 4:
            ch = (int(reg, 16) - 0x06) // 4
            w = [int(v, 16) for v in vals[:4]]
            if w[3] & 0x10:
                lvl = 0
            elif w[1] & 0x10:
                lvl = 4095
            else:
                on = ((w[1] & 0x0F) << 8) | w[0]
                lvl = ((((w[3] & 0x0F) << 8) | w[2]) - on) & 0xFFF
            pwm.setdefault(str(ch), {}).update(level=lvl)
    elif vals:
        regs = s.setdefault("i2c", {}).setdefault(addr, {})
        for offset, value in enumerate(vals):
            regs[hex(int(reg, 16) ^ (offset % 2))] = int(value, 16)
    save(s)

elif TOOL == "gpio":
    if a[0] == "read":
        print(s["gpio"].get(a[1], 0))
    elif a[0] == "write":
        s.setdefault("gpio", {})[a[1]] = int(a[2])
        save(s)
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def api(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def find_chrome(explicit):
    if explicit:
        return explicit
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    for pat in ("~/.cache/ms-playwright/chromium*/chrome-linux*/chrome",
                "~/.cache/puppeteer/chrome/*/chrome-linux*/chrome"):
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    return None


def build_sandbox(tmp, backpack=None):
    """Lay out the stub PATH, static files and demo config."""
    binp, panel = tmp / "bin", tmp / "panel"
    (panel / "static").mkdir(parents=True)
    binp.mkdir()
    shutil.copy(REPO / "static" / "index.html", panel / "static" / "index.html")
    config = dict(DEMO_CONFIG)
    if backpack is not None:
        config["backpacks"] = [{"id": 1, "name": "T31 on backpack", "host": "127.0.0.1",
                                "mdns": backpack.name, "http_port": backpack.http_port,
                                "console_port": backpack.console_port}]
    (panel / "config.json").write_text(json.dumps(config, indent=2))
    stub = binp / "_stub.py"
    stub.write_text(STUB)
    stub.chmod(0o755)
    for tool in ("i2ctransfer", "i2cget", "i2cset", "i2cdump", "gpio"):
        (binp / tool).symlink_to("_stub.py")
    return binp, panel


def seed(port):
    """Put the demo into the state that makes a good screenshot."""
    for board, relay in DEMO_RELAYS:
        api(port, "POST", "/api/relay/set",
            {"board": board, "relay": relay, "state": True})
    api(port, "POST", "/api/pwm/set", {"chip": 0, "channel": 0, "level": DEMO_WHITE})
    api(port, "POST", "/api/pwm/group", {"chip": 0, "group": 0, "level": DEMO_GROUP})


class FakeBackpack:
    """A thingino backpack's HTTP API and console, on local ports.

    Mirrors the unit's answers: /api/camera and its actions, /api/ota, and a
    raw console that serves one client and tells the old one who took over.
    """

    def __init__(self, name="thingino-backpack-demo"):
        self.name = name
        self.state = {"power": "on", "boot_pin": "released", "usb": [],
                      "bootsel": "low", "usb_port": "on"}
        self.lock = threading.Lock()
        self.client = None
        self.stop = threading.Event()
        backpack = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/api/camera":
                    self.reply(backpack.camera())
                elif path == "/api/ota":
                    self.reply({"version": "demo-0.2.0", "slot": "ota_0", "state": "valid"})
                else:
                    self.reply({"ok": False, "error": "not found"}, 404)

            def do_POST(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path != "/api/camera":
                    self.reply({"ok": False, "error": "not found"}, 404)
                    return
                ok, message = backpack.act(query)
                self.reply({"ok": True, "message": message} if ok else {"ok": False, "error": message},
                           200 if ok else 400)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_port = self.http.server_address[1]
        self.console = socket.socket()
        self.console.bind(("127.0.0.1", 0))
        self.console.listen(4)
        self.console_port = self.console.getsockname()[1]
        for target in (self.http.serve_forever, self._accept, self._chatter):
            threading.Thread(target=target, daemon=True).start()

    def camera(self):
        with self.lock:
            s = dict(self.state)
        return {"power": s["power"], "boot_pin": s["boot_pin"], "usb_devices": len(s["usb"]),
                "usb": list(s["usb"]), "stuck_s": None, "recoveries": 0, "flash_lent": False,
                "bootsel": s["bootsel"], "usb_port": s["usb_port"]}

    def act(self, query):
        with self.lock:
            s = self.state
            if "bootsel" in query:
                s["bootsel"] = query["bootsel"]
                return True, f"BOOTSEL {query['bootsel']}"
            if "usb_port" in query:
                s["usb_port"] = query["usb_port"]
                return True, f"USB port {query['usb_port']}"
            action = query.get("action")
            if action == "power-on":
                s["power"], s["usb"] = "on", []
            elif action == "power-off":
                s["power"], s["usb"] = "off", []
            elif action == "power-cycle":
                s["power"], s["usb"] = "on", []
            elif action == "bootrom":
                s["power"], s["boot_pin"], s["usb"] = "on", "released", ["a108:c309"]
            elif action == "boot-hold":
                s["boot_pin"] = "held"
            elif action == "boot-release":
                s["boot_pin"] = "released"
            else:
                return False, "unknown action"
            return True, action
        
    def _accept(self):
        while not self.stop.is_set():
            try:
                client, peer = self.console.accept()
            except OSError:
                return
            with self.lock:
                old, self.client = self.client, client
            if old is not None:
                try:
                    old.sendall(f"\r\n[console taken over by {peer[0]}]\r\n".encode())
                    old.close()
                except OSError:
                    pass

    def _chatter(self):
        n = 0
        while not self.stop.wait(1.0):
            n += 1
            with self.lock:
                client, power = self.client, self.state["power"]
            if client is None or power != "on":
                continue
            try:
                client.sendall(f"{self.name}: camera console line {n}\r\n".encode())
            except OSError:
                with self.lock:
                    if self.client is client:
                        self.client = None

    def close(self):
        self.stop.set()
        self.http.shutdown()
        self.console.close()


class DemoUarts:
    """24 local, simulated raw TCP consoles; no physical UARTs are opened.

    Each answers typing like a minimal shell: echo, backspace redrawn the way
    busybox ash does it (\\b ESC[J), Ctrl-C, and a reply per line.
    """

    def __init__(self):
        self.selector = selectors.DefaultSelector()
        self.listeners = []
        self.clients = {}
        self.lines = {}
        self.escapes = {}
        self.quiet_until = {}
        self.stop = threading.Event()
        for attempt in range(20):
            self.base_port = free_port()
            if self.base_port > 65512:
                continue
            try:
                for index in range(24):
                    listener = socket.socket()
                    self.listeners.append(listener)
                    listener.bind(("127.0.0.1", self.base_port + index))
                    listener.listen(8)
                    listener.setblocking(False)
            except OSError:
                for listener in self.listeners:
                    listener.close()
                self.listeners.clear()
            else:
                break
        else:
            self.selector.close()
            raise RuntimeError("Could not allocate simulated UART ports")
        for index, listener in enumerate(self.listeners):
            self.selector.register(listener, selectors.EVENT_READ, ("listen", index))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _drop(self, client):
        self.selector.unregister(client)
        client.close()
        del self.clients[client]

    def _echo(self, index, text):
        # A device echoes to every ser2net client, not just the one typing.
        for client, owner in list(self.clients.items()):
            if owner == index:
                try:
                    client.sendall(text.encode())
                except OSError:
                    self._drop(client)

    def _typed(self, index, data):
        # Chatter pauses while someone types, so a half-typed line stays whole.
        self.quiet_until[index] = time.monotonic() + 3
        line = self.lines.setdefault(index, [])
        escape = self.escapes.get(index, "")
        for char in data.decode(errors="replace"):
            # Arrow and editing keys arrive as escape sequences; ignore them.
            if escape == "esc":
                escape = "csi" if char == "[" else ""
            elif escape == "csi":
                escape = "" if "@" <= char <= "~" else "csi"
            elif char == "\x1b":
                escape = "esc"
            elif char in "\r\n":
                command = "".join(line)
                line.clear()
                reply = f"demo: ran '{command}'\r\n" if command else ""
                self._echo(index, f"\r\n{reply}demo$ ")
            elif char in "\x7f\b":
                if line:
                    line.pop()
                    self._echo(index, "\b\x1b[J")
            elif char == "\x03":
                line.clear()
                self._echo(index, "^C\r\ndemo$ ")
            elif char >= " ":
                line.append(char)
                self._echo(index, char)
        self.escapes[index] = escape

    def _run(self):
        tick, next_output = 0, time.monotonic()
        while not self.stop.is_set():
            for key, _ in self.selector.select(timeout=0.1):
                kind, index = key.data
                if kind == "listen":
                    client, _ = key.fileobj.accept()
                    client.settimeout(0.2)
                    self.clients[client] = index
                    self.selector.register(client, selectors.EVENT_READ, ("client", index))
                    continue
                try:
                    data = key.fileobj.recv(4096)
                except OSError:
                    data = b""
                if data:
                    self._typed(index, data)
                elif key.fileobj in self.clients:
                    self._drop(key.fileobj)
            if time.monotonic() < next_output:
                continue
            tick += 1
            next_output = time.monotonic() + 0.5
            now = time.monotonic()
            for client, index in list(self.clients.items()):
                if self.lines.get(index) or now < self.quiet_until.get(index, 0):
                    continue
                try:
                    client.sendall(f"\x1b[32mttyUSB{index} demo\x1b[0m: console output {tick}\r\n".encode())
                except OSError:
                    self._drop(client)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
        for client in self.clients:
            client.close()
        for listener in self.listeners:
            listener.close()
        self.selector.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--screenshot", metavar="PATH",
                    help="capture to PATH and exit instead of serving")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1500)
    ap.add_argument("--chrome", help="path to a Chromium binary")
    ap.add_argument("--usb-vid", default="",
                    help="watch this USB vendor so the USB section has content "
                         "(default: none, section renders empty)")
    args = ap.parse_args()

    port = args.port or free_port()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="relay-panel-demo-"))
    backpack = FakeBackpack()
    binp, panel = build_sandbox(tmp, backpack)
    uarts = DemoUarts()

    env = dict(os.environ)
    env["PATH"] = f"{binp}:{env['PATH']}"
    env["STUB_STATE"] = str(tmp / "state.json")
    env["RELAY_PANEL_DIR"] = str(panel)
    env["RELAY_PANEL_PORT"] = str(port)
    env["RELAY_PANEL_HOST"] = "127.0.0.1"
    env["RELAY_PANEL_UART_HOST"] = "127.0.0.1"
    env["RELAY_PANEL_BACKPACK_DISCOVERY"] = "0"
    env["RELAY_PANEL_UART_BASE_PORT"] = str(uarts.base_port)
    env["RELAY_PANEL_UART_COUNT"] = "24"
    if args.usb_vid:
        env["RELAY_PANEL_USB_VID"] = args.usb_vid

    proc = subprocess.Popen([sys.executable, str(REPO / "relay-panel.py")],
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            try:
                api(port, "GET", "/api/state")
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                if proc.poll() is not None:
                    print(proc.stdout.read(), file=sys.stderr)
                    return 1
                time.sleep(0.25)
        else:
            print("panel did not start", file=sys.stderr)
            return 1

        seed(port)

        if not args.screenshot:
            print(f"demo panel on http://127.0.0.1:{port}   (ctrl-c to stop)", flush=True)
            print(f"sandbox: {tmp}", flush=True)
            signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            proc.wait()
            return 0

        chrome = find_chrome(args.chrome)
        if not chrome:
            print("no chromium found; pass --chrome PATH", file=sys.stderr)
            return 1
        out = pathlib.Path(args.screenshot).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = tmp / "raw.png"
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=2",
             f"--window-size={args.width},{args.height}",
             "--virtual-time-budget=6000", f"--screenshot={raw}",
             f"http://127.0.0.1:{port}/"],
            check=True, capture_output=True)

        try:
            from PIL import Image
            im = Image.open(raw).convert("RGB")
            im.resize((args.width, round(im.height * args.width / im.width)),
                      Image.LANCZOS).save(out, optimize=True)
        except ImportError:
            shutil.copy(raw, out)          # keep the 2x image if Pillow is absent
        print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")
        return 0
    finally:
        uarts.close()
        backpack.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
