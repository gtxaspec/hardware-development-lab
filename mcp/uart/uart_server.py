#!/usr/bin/env python3
"""
MCP server for UART read/write via ser2net TCP connections.

Persistent connection per device — all tools share it.
Background reader fills a buffer; log taps the same stream.

ser2net on the bench controller maps ttyUSB ports to TCP:
  ttyUSBN -> tcp://UART_HOST:UART_BASE_PORT+N
"""

import json
import os
import re
import socket
import threading
import time
from mcp.server.fastmcp import FastMCP

UART_HOST = os.environ.get("UART_HOST", "localhost")
UART_BASE_PORT = int(os.environ.get("UART_BASE_PORT", "3000"))
UART_COUNT = int(os.environ.get("UART_COUNT", "24"))
LOG_DIR = "/tmp/uart-logs"
ALIASES_FILE = os.environ.get(
    "UART_ALIASES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.json"))

# Port N is ttyUSBN on UART_HOST:UART_BASE_PORT+N. Names are optional aliases
# from ALIASES_FILE, re-read on every call so they can change without a
# restart. An alias maps to a port index, "ttyUSBN", or {"host", "port"} for
# consoles that are not on the ser2net box (e.g. a local QEMU serial socket).


def _load_aliases() -> dict:
    try:
        with open(ALIASES_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {ALIASES_FILE}: {e}")
    return {k.lower(): v for k, v in data.items() if not k.startswith("_")}


def _save_aliases(aliases: dict):
    tmp = ALIASES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(sorted(aliases.items())), f, indent=2)
        f.write("\n")
    os.replace(tmp, ALIASES_FILE)


def _parse_index(ref) -> int | None:
    """0-based index from 5, "5", "ttyUSB5" or the TCP port 3005."""
    if isinstance(ref, int):
        n = ref
    else:
        m = re.fullmatch(r"(?:ttyusb)?(\d+)", str(ref).strip().lower())
        if not m:
            return None
        n = int(m.group(1))
    if 0 <= n < UART_COUNT:
        return n
    if UART_BASE_PORT <= n < UART_BASE_PORT + UART_COUNT:
        return n - UART_BASE_PORT
    return None


def _resolve(ref: str) -> dict:
    """Map a device reference to {key, label, host, port, uart}.

    key identifies the physical console, so an alias and its raw port share
    one connection and one log; label is the alias when the port has one.
    """
    aliases = _load_aliases()
    name = str(ref).strip().lower()
    target = aliases.get(name, ref)
    if isinstance(target, dict):
        return {"key": name, "label": name, "host": target.get("host", UART_HOST),
                "port": int(target["port"]), "uart": target.get("uart", f"tcp:{target['port']}")}
    idx = _parse_index(target)
    if idx is None:
        if name in aliases:
            raise ValueError(f"alias '{name}' points at '{target}', not a port 0-{UART_COUNT - 1}")
        raise ValueError(
            f"Unknown device '{ref}'. Use a port (0-{UART_COUNT - 1}, ttyUSBN or "
            f"{UART_BASE_PORT}-{UART_BASE_PORT + UART_COUNT - 1}) or an alias: "
            f"{', '.join(sorted(aliases)) or 'none defined'}")
    names = sorted(a for a, t in aliases.items()
                   if not isinstance(t, dict) and _parse_index(t) == idx)
    return {"key": f"ttyUSB{idx}", "label": names[0] if names else f"ttyUSB{idx}",
            "host": UART_HOST, "port": UART_BASE_PORT + idx, "uart": f"ttyUSB{idx}"}


os.makedirs(LOG_DIR, exist_ok=True)

MAX_BUFFER = 2_000_000
TRIM_TO = 1_000_000


def _decode_escapes(s: str) -> bytes:
    """Decode \\xNN, \\n, \\r, \\t, \\\\ escape sequences into raw bytes."""
    result = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            c = s[i + 1]
            if c == "x" and i + 3 < len(s):
                try:
                    result.append(int(s[i + 2 : i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            elif c == "n":
                result.append(0x0A)
                i += 2
                continue
            elif c == "r":
                result.append(0x0D)
                i += 2
                continue
            elif c == "t":
                result.append(0x09)
                i += 2
                continue
            elif c == "\\":
                result.append(0x5C)
                i += 2
                continue
        result.extend(s[i].encode("utf-8"))
        i += 1
    return bytes(result)


class DeviceConnection:
    """Persistent TCP connection with background reader."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = None
        self.buffer = b""
        self.lock = threading.Lock()
        self.running = False
        self._log_fh = None
        self._log_lock = threading.Lock()

    @property
    def alive(self):
        return self.running and self.sock is not None

    def connect(self):
        if self.alive:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(5)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.1)
        self.running = True
        with self.lock:
            self.buffer = b""
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()

    def _reader(self):
        while self.running:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                with self.lock:
                    self.buffer += data
                    if len(self.buffer) > MAX_BUFFER:
                        self.buffer = self.buffer[-TRIM_TO:]
                with self._log_lock:
                    if self._log_fh:
                        self._log_fh.write(data.decode("utf-8", errors="replace"))
                        self._log_fh.flush()
            except socket.timeout:
                continue
            except OSError:
                break
        self.running = False

    def write(self, data: bytes):
        if not self.alive:
            self.connect()
        self.sock.sendall(data)

    def get_mark(self) -> int:
        """Current buffer byte offset, for cursor-based reading."""
        with self.lock:
            return len(self.buffer)

    def clear_buffer(self):
        """Discard all buffered data."""
        with self.lock:
            self.buffer = b""

    def wait_for(self, pattern: str, timeout: float, lookback: int = 0,
                 from_mark: int | None = None) -> tuple:
        """Wait for regex in output. Returns (matched: bool, captured: str).

        lookback: bytes to search backwards from current position.
                  0 = only new data, -1 = entire buffer.
        from_mark: explicit byte offset to scan from (overrides lookback).
        """
        compiled = re.compile(pattern)
        start = time.monotonic()
        with self.lock:
            if from_mark is not None:
                mark = from_mark
            elif lookback < 0:
                mark = 0
            elif lookback > 0:
                mark = max(0, len(self.buffer) - lookback)
            else:
                mark = len(self.buffer)
        while time.monotonic() - start < timeout:
            with self.lock:
                new_data = self.buffer[mark:]
            text = new_data.decode("utf-8", errors="replace")
            m = compiled.search(text)
            if m:
                return True, text[: m.end()]
            time.sleep(0.05)
        with self.lock:
            text = self.buffer[mark:].decode("utf-8", errors="replace")
        return False, text

    def read_new(self, since_mark: int = 0) -> tuple:
        """Read buffer content since mark. Returns (text, new_mark)."""
        with self.lock:
            data = self.buffer[since_mark:]
            return data.decode("utf-8", errors="replace"), len(self.buffer)

    def start_log(self, path: str):
        with self._log_lock:
            if self._log_fh:
                self._log_fh.close()
            self._log_fh = open(path, "w")
        with self.lock:
            self.buffer = b""

    def stop_log(self):
        with self._log_lock:
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None

    @property
    def logging(self):
        return self._log_fh is not None

    def close(self):
        self.running = False
        self.stop_log()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


_connections: dict[str, DeviceConnection] = {}


def _get_conn(dev: dict) -> DeviceConnection:
    """Get or create the persistent connection for a resolved device."""
    conn = _connections.get(dev["key"])
    if conn and conn.alive:
        return conn
    if conn:
        conn.close()
    conn = DeviceConnection(dev["host"], dev["port"])
    conn.connect()
    _connections[dev["key"]] = conn
    return conn


def _log_path(dev: dict, suffix: str = "") -> str:
    return os.path.join(LOG_DIR, f"{dev['label']}{suffix}.log")


mcp = FastMCP("uart")


# ── Write tools ──────────────────────────────────────────────


@mcp.tool()
def write_uart(
    device: str, data: str, repeat: int = 1, interval: float = 0.0
) -> str:
    """Send data to a device's UART.

    Supports escape sequences: \\x03 (Ctrl-C), \\r, \\n, \\x1b (ESC).
    A carriage return is appended automatically unless data already
    ends with \\r or \\n, or `data` is a single control character.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        data: String to send (escape sequences supported)
        repeat: Send the data this many times (default 1)
        interval: Seconds between repeats (default 0 = no delay)
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        raw = _decode_escapes(data)
        if len(raw) > 1 and not raw.endswith((b"\r", b"\n")):
            raw += b"\r"
        for i in range(repeat):
            conn.write(raw)
            if interval > 0 and i < repeat - 1:
                time.sleep(interval)
        total = len(raw) * repeat
        return f"Sent {total} bytes to {device} ({repeat}x)"
    except Exception as e:
        return f"Error writing to {device}: {e}"


@mcp.tool()
def uart_expect(
    device: str,
    pattern: str,
    timeout: float = 5.0,
    send: str | None = None,
) -> str:
    """Send data (optional) and wait for a regex pattern in UART output.

    Returns captured output up to the match. If no match within timeout,
    returns what was captured prefixed with [TIMEOUT].

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        pattern: Python regex to wait for in output
        timeout: Max seconds to wait (default 5)
        send: Optional data to send first (escape sequences supported).
              A carriage return is appended unless it ends with \\r/\\n.
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        mark = None
        if send is not None:
            raw = _decode_escapes(send)
            if len(raw) > 1 and not raw.endswith((b"\r", b"\n")):
                raw += b"\r"
            mark = conn.get_mark()
            conn.write(raw)
        matched, text = conn.wait_for(pattern, timeout, from_mark=mark)
        if matched:
            return text
        return f"[TIMEOUT after {timeout}s]\n{text}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def uart_interact(device: str, script: list[dict], timeout: float = 30.0,
                  clear: bool = False) -> str:
    """Execute a multi-step UART interaction.

    Each step is a dict with optional keys:
      wait:       regex — wait for this pattern before proceeding
      send:       string — data to send (escape sequences supported,
                  auto \\r appended unless ends with \\r/\\n or is control char)
      expect:     regex — wait for this pattern after sending
      send_until: string — send repeatedly until expect pattern matches
      interval:   float — seconds between send_until repeats (default 0.2)
      delay:      float — seconds to pause before this step

    Steps run sequentially. If any wait/expect times out, the
    interaction stops and returns the transcript so far with a
    [TIMEOUT] marker.

    Returns the full captured transcript.

    Example — break into U-Boot and run a command:
      [
        {"send_until": "\\x03", "expect": "#|=>", "interval": 0.1},
        {"send": "printenv bootargs", "expect": "#|=>"}
      ]

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        script: List of step dicts
        timeout: Max seconds per wait/expect (default 30)
        clear: Clear UART buffer before starting (default false)
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        if clear:
            conn.clear_buffer()
        transcript = ""
        mark = conn.get_mark()

        for i, step in enumerate(script):
            if "delay" in step:
                time.sleep(step["delay"])

            if "send_until" in step:
                raw = _decode_escapes(step["send_until"])
                pattern = step.get("expect", ".")
                interval = step.get("interval", 0.2)
                compiled = re.compile(pattern)
                start = time.monotonic()
                matched = False

                while time.monotonic() - start < timeout:
                    with conn.lock:
                        new_data = conn.buffer[mark:]
                    text = new_data.decode("utf-8", errors="replace")
                    m = compiled.search(text)
                    if m:
                        transcript += text[: m.end()]
                        mark = conn.get_mark()
                        matched = True
                        break
                    conn.write(raw)
                    time.sleep(interval)

                if not matched:
                    time.sleep(0.1)
                    with conn.lock:
                        text = conn.buffer[mark:].decode("utf-8", errors="replace")
                    m = compiled.search(text)
                    if m:
                        transcript += text[: m.end()]
                        mark = conn.get_mark()
                        matched = True

                if not matched:
                    with conn.lock:
                        text = conn.buffer[mark:].decode("utf-8", errors="replace")
                    return f"{transcript}{text}\n[TIMEOUT step {i}: send_until '{pattern}']"
                continue

            if "wait" in step:
                matched, text = conn.wait_for(step["wait"], timeout, from_mark=mark)
                transcript += text
                mark = conn.get_mark()
                if not matched:
                    return f"{transcript}\n[TIMEOUT step {i}: waiting for '{step['wait']}']"

            if "send" in step:
                raw = _decode_escapes(step["send"])
                if len(raw) > 1 and not raw.endswith((b"\r", b"\n")):
                    raw += b"\r"
                mark = conn.get_mark()
                conn.write(raw)

            if "expect" in step:
                matched, text = conn.wait_for(step["expect"], timeout, from_mark=mark)
                transcript += text
                mark = conn.get_mark()
                if not matched:
                    return f"{transcript}\n[TIMEOUT step {i}: expecting '{step['expect']}']"

        return transcript if transcript else "OK (no output captured)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def clear_uart_buffer(device: str) -> str:
    """Clear the in-memory UART buffer for a device.

    Use before starting a new interaction to discard stale data
    from previous boots or sessions.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        with conn.lock:
            size = len(conn.buffer)
        conn.clear_buffer()
        return f"Cleared {size} bytes from {device} buffer"
    except Exception as e:
        return f"Error: {e}"


# ── Logging tools (use persistent connection) ────────────────


@mcp.tool()
def start_uart_log(device: str) -> str:
    """Start background UART capture to a log file.

    Runs in the background, capturing all UART output to /tmp/uart-logs/<alias or ttyUSBN>.log.
    Start this BEFORE bootstrap/flash to capture boot logs.
    Use get_uart_log() to read the captured output.
    Use stop_uart_log() to stop.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        log_file = _log_path(dev)
        conn.start_log(log_file)
        return (
            f"UART logging started for {device} ({dev['uart']}) → {log_file}"
        )
    except Exception as e:
        return f"Error starting log for {device}: {e}"


@mcp.tool()
def fresh_uart_log(device: str, tag: str = "") -> str:
    """Rotate the current UART log and restart capture with an empty file.

    Saves the current log to /tmp/uart-logs/<device>-<tag>.log (or
    auto-timestamped if no tag), then restarts logging with a clean
    file. The connection stays up - no ser2net churn.

    Call this BEFORE each dev cycle (power-on, USB-boot, flash) so
    get_uart_log only returns output from the current cycle.

    If no logger is running, starts one.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        tag: Optional label for the saved log (e.g. 'cycle-3', 'vendor-boot')
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        log_file = _log_path(dev)

        saved = None
        if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
            if tag:
                save_name = f"{dev['label']}-{tag}.log"
            else:
                ts = time.strftime("%Y%m%d-%H%M%S")
                save_name = f"{dev['label']}-{ts}.log"
            save_path = os.path.join(LOG_DIR, save_name)
            was_logging = conn.logging
            if was_logging:
                conn.stop_log()
            os.rename(log_file, save_path)
            saved = save_path
            conn.start_log(log_file)
        else:
            conn.start_log(log_file)
        msg = f"UART fresh for {device} ({dev['uart']}) → {log_file}"
        if saved:
            msg += f"\nPrevious log saved: {saved}"
        return msg
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def stop_uart_log(device: str) -> str:
    """Stop background UART capture.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    conn = _connections.get(dev["key"])
    if not conn or not conn.logging:
        return f"No active UART capture for {device}"
    conn.stop_log()
    log_file = _log_path(dev)
    size = os.path.getsize(log_file) if os.path.exists(log_file) else 0
    return f"UART logging stopped for {device}. Log: {log_file} ({size} bytes)"


@mcp.tool()
def get_uart_log(device: str, lines: int = 0) -> str:
    """Read the captured UART log file.

    Returns the full log, or last N lines if specified.
    Works whether the capture is still running or stopped.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        lines: Number of lines from end (0 = all, default 0)
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    log_file = _log_path(dev)
    if not os.path.exists(log_file):
        return f"No UART log for {device}. Use start_uart_log() first."
    try:
        with open(log_file, "r", errors="replace") as f:
            text = f.read().strip()
    except Exception as e:
        return f"Error reading log: {e}"
    if not text:
        conn = _connections.get(dev["key"])
        logging = conn and conn.logging
        status = "capture running, no output yet" if logging else "log is empty"
        return f"No UART output for {device} ({status})"
    if lines > 0:
        all_lines = text.split("\n")
        if len(all_lines) > lines:
            return "\n".join(all_lines[-lines:])
    return text


# ── Read tools ───────────────────────────────────────────────


@mcp.tool()
def read_uart(device: str, timeout: float = 2.0) -> str:
    """Read live UART output from a device.

    Waits for `timeout` seconds collecting output, then returns it.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        timeout: Seconds to read for (default 2.0)
    """
    try:
        dev = _resolve(device)
    except ValueError as e:
        return str(e)
    try:
        conn = _get_conn(dev)
        _, mark = conn.read_new(0)
        time.sleep(timeout)
        text, _ = conn.read_new(mark)
        if not text.strip():
            return f"No UART output from {device} ({dev['uart']}) within {timeout}s"
        return text.strip()
    except Exception as e:
        return f"Error reading {device} UART: {e}"


@mcp.tool()
def read_uart_lines(device: str, lines: int = 50, timeout: float = 5.0) -> str:
    """Read UART output and return the last N lines.

    Args:
        device: Port (5, ttyUSB5 or 3005) or an alias from list_uart_devices
        lines: Number of lines to return from the end (default 50)
        timeout: Seconds to read for (default 5.0)
    """
    text = read_uart(device, timeout)
    if text.startswith(("Unknown", "alias", "cannot read", "Connection", "Error", "No UART")):
        return text
    all_lines = text.split("\n")
    if len(all_lines) > lines:
        return "\n".join(all_lines[-lines:])
    return text


@mcp.tool()
def list_uart_devices() -> str:
    """List every UART port, its aliases, and connection status."""
    try:
        aliases = _load_aliases()
    except ValueError as e:
        return str(e)
    by_idx: dict[int, list[str]] = {}
    extra = []
    for name, target in sorted(aliases.items()):
        idx = None if isinstance(target, dict) else _parse_index(target)
        if idx is None:
            extra.append((name, target))
        else:
            by_idx.setdefault(idx, []).append(name)

    def status(key):
        conn = _connections.get(key)
        if not (conn and conn.alive):
            return "idle"
        return "connected, logging" if conn.logging else "connected"

    out = (f"{UART_COUNT} UART ports on {UART_HOST}, TCP {UART_BASE_PORT}-"
           f"{UART_BASE_PORT + UART_COUNT - 1}; aliases: {ALIASES_FILE}\n\n")
    out += f"{'UART':<10} {'Port':<6} {'Aliases':<20} Status\n"
    for i in range(UART_COUNT):
        out += (f"{'ttyUSB%d' % i:<10} {UART_BASE_PORT + i:<6} "
                f"{', '.join(by_idx.get(i, [])) or '-':<20} {status(f'ttyUSB{i}')}\n")
    for name, target in extra:
        if isinstance(target, dict):
            out += (f"{name:<10} {target.get('port', '?'):<6} "
                    f"{target.get('host', UART_HOST):<20} {status(name)}\n")
        else:
            out += f"{name:<10} {'?':<6} {'BAD TARGET ' + str(target):<20}\n"
    return out


@mcp.tool()
def name_uart(name: str, port: str = "") -> str:
    """Give a UART port an optional alias, or remove one.

    Names are only shortcuts: every tool also takes the bare port. Verify
    which board is on a port (watch its boot banner) before naming it.

    Args:
        name: Alias to set or remove (case-insensitive)
        port: Port (5, ttyUSB5 or 3005); empty removes the alias
    """
    name = name.strip().lower()
    if not name or _parse_index(name) is not None:
        return f"'{name}' is not usable as an alias: it reads as a port"
    try:
        aliases = _load_aliases()
    except ValueError as e:
        return str(e)
    if not port:
        if aliases.pop(name, None) is None:
            return f"No alias '{name}'"
        _save_aliases(aliases)
        return f"Removed alias '{name}'"
    idx = _parse_index(port)
    if idx is None:
        return f"'{port}' is not a port 0-{UART_COUNT - 1}"
    old = aliases.get(name)
    aliases[name] = f"ttyUSB{idx}"
    _save_aliases(aliases)
    return f"{name} -> ttyUSB{idx} (TCP {UART_BASE_PORT + idx})" + (
        f", was {old}" if old else "")


if __name__ == "__main__":
    mcp.run()
