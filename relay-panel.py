#!/usr/bin/env python3
"""Relay Control Panel - Web interface for XL9535 I2C relay boards."""

import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path("/opt/relay-panel")
CONFIG_FILE = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

i2c_lock = threading.Lock()
config_lock = threading.Lock()


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"boards": []}


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
            self.send_json({"boards": state})
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

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), RelayHandler)
    print(f"Relay Control Panel running on http://{LISTEN_HOST}:{LISTEN_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
