"""thingino backpacks: ESP32 units that each own one camera's power, boot pin,
console and USB port, reached over the network (standard library only).

The panel polls each unit's HTTP API from a thread of its own, so a unit that
drops off Wi-Fi shows up as offline without ever stalling the I2C sampler,
and proxies actions to it. Units are found by a one-shot mDNS query.
"""

import ipaddress
import json
import select
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

POLL_INTERVAL = 1.0
POLL_TIMEOUT = 2.0
VERSION_EVERY = 60.0
# `bootrom` waits up to 10 s for the bootrom to enumerate before answering.
ACTION_TIMEOUT = 25.0
STALE_AFTER = 5.0
ACTIONS = ("power-on", "power-off", "power-cycle", "bootrom", "boot-hold", "boot-release")
SETTINGS = {"bootsel": ("low", "high"), "usb_port": ("on", "off")}
HOSTNAME_PREFIX = "thingino-backpack-"
SERVICE = "_thingino._tcp.local"
# What a unit's console sends the client it drops for a newer one.
TAKEOVER = b"[console taken over by"


def url_host(address):
    try:
        if ipaddress.ip_address(address).version == 6:
            return f"[{address}]"
    except ValueError:
        pass
    return address


def http_json(address, port, path, method="GET", timeout=POLL_TIMEOUT):
    request = urllib.request.Request(f"http://{url_host(address)}:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        # The unit answers refusals as JSON with an error status.
        try:
            return json.loads(error.read() or b"{}")
        except ValueError:
            return {"ok": False, "error": f"HTTP {error.code}"}


class Unit:
    """One backpack: its config entry, last reading, and poll thread."""

    def __init__(self, entry, on_change):
        self.entry = entry
        self.on_change = on_change
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = False
        self.camera = None
        self.version = None
        self.error = "not polled yet"
        self.read_at = None
        self.version_at = 0.0
        self.address = None
        self.thread = threading.Thread(target=self.run, name=f"backpack-{entry['id']}", daemon=True)

    def addresses(self):
        return self.entry.get("addresses") or [self.entry["host"]]

    def request(self, path, method="GET", timeout=POLL_TIMEOUT):
        """Try the address that answered last, then the others."""
        ordered = self.addresses()
        if self.address in ordered:
            ordered = [self.address] + [a for a in ordered if a != self.address]
        error = None
        for address in ordered:
            try:
                answer = http_json(address, self.entry.get("http_port", 80), path, method, timeout)
                self.address = address
                return answer
            except (OSError, ValueError) as e:
                error = e
        raise OSError(f"{self.entry['host']}: {getattr(error, 'reason', error)}")

    def poll(self):
        try:
            camera = self.request("/api/camera")
            if time.monotonic() - self.version_at > VERSION_EVERY:
                self.version_at = time.monotonic()
                try:
                    self.version = self.request("/api/ota").get("version")
                except OSError:
                    pass
            with self.lock:
                changed = camera != self.camera or self.error is not None
                self.camera, self.error, self.read_at = camera, None, time.monotonic()
        except OSError as e:
            with self.lock:
                changed = self.error is None
                self.error = str(e)
        if changed:
            self.on_change()

    def run(self):
        while not self.stopped:
            self.poll()
            self.wake.wait(POLL_INTERVAL)
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            camera, error, read_at = self.camera or {}, self.error, self.read_at
        age = None if read_at is None else time.monotonic() - read_at
        e = self.entry
        return {
            "id": e["id"],
            "name": e.get("name") or e.get("mdns") or e["host"],
            "host": e["host"],
            "mdns": e.get("mdns"),
            "address": self.address,
            "online": error is None,
            "stale": age is None or age > STALE_AFTER,
            "error": error,
            "version": self.version,
            "power": camera.get("power"),
            "boot_pin": camera.get("boot_pin"),
            "usb": camera.get("usb", []),
            "stuck_s": camera.get("stuck_s"),
            "recoveries": camera.get("recoveries"),
            "bootsel": camera.get("bootsel"),
            "usb_port": camera.get("usb_port"),
            "flash_lent": camera.get("flash_lent"),
            "dfu": f"{url_host(self.address or e['host'])}:{e.get('dfu_port', 5050)}",
        }


class BackpackMonitor:
    def __init__(self, on_change=lambda: None):
        self.on_change = on_change
        self.lock = threading.Lock()
        self.units = {}

    def sync(self, entries):
        """Match the running pollers to the configured units."""
        wanted = {e["id"]: e for e in entries}
        with self.lock:
            for uid in list(self.units):
                if uid not in wanted:
                    self.units.pop(uid).stopped = True
            for uid, entry in wanted.items():
                unit = self.units.get(uid)
                if unit is None:
                    unit = self.units[uid] = Unit(entry, self.on_change)
                    unit.thread.start()
                else:
                    unit.entry = entry
                    unit.wake.set()

    def snapshot(self):
        with self.lock:
            units = sorted(self.units.values(), key=lambda u: u.entry["id"])
        return [unit.snapshot() for unit in units]

    def unit(self, uid):
        with self.lock:
            return self.units.get(uid)

    def call(self, uid, query):
        """POST /api/camera?<query> on a unit; returns (http status, body)."""
        unit = self.unit(uid)
        if unit is None:
            return 404, {"error": f"no backpack {uid}"}
        try:
            answer = unit.request(f"/api/camera?{query}", "POST", ACTION_TIMEOUT)
        except OSError as e:
            return 502, {"ok": False, "error": str(e)}
        unit.wake.set()
        return (200 if answer.get("ok", True) else 409), answer


# --- mDNS: one query, answers collected for a moment (RFC 6762 one-shot) ---

def _name(text):
    return b"".join(bytes([len(label)]) + label.encode() for label in text.rstrip(".").split(".")) + b"\0"


def _query(*questions):
    body = b"".join(_name(name) + struct.pack("!HH", qtype, 1) for name, qtype in questions)
    return struct.pack("!6H", 0, 0, len(questions), 0, 0, 0) + body


def _read_name(data, offset):
    labels, end, hops = [], None, 0
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            hops += 1
            if hops > 32:
                raise ValueError("mDNS name loop")
            continue
        offset += 1
        if length == 0:
            break
        labels.append(data[offset:offset + length].decode(errors="replace"))
        offset += length
    return ".".join(labels), (end if end is not None else offset)


def parse_records(data):
    """Every resource record in a DNS message, as (name, type, value) tuples."""
    _, _, qd, an, ns, ar = struct.unpack("!6H", data[:12])
    offset = 12
    for _ in range(qd):
        _, offset = _read_name(data, offset)
        offset += 4
    records = []
    for _ in range(an + ns + ar):
        name, offset = _read_name(data, offset)
        rtype, _, _, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + length]
        if rtype == 12:
            records.append((name, "PTR", _read_name(data, offset)[0]))
        elif rtype == 33:
            records.append((name, "SRV", _read_name(data, offset + 6)[0]))
        elif rtype == 1 and length == 4:
            records.append((name, "A", socket.inet_ntop(socket.AF_INET, rdata)))
        elif rtype == 28 and length == 16:
            records.append((name, "AAAA", socket.inet_ntop(socket.AF_INET6, rdata)))
        offset += length
    return records


def _sockets():
    """One socket per family; IPv6 sends once per interface (ff02::fb is link-scoped)."""
    out = []
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s6.bind(("::", 0))
        out.append((s6, [("ff02::fb", 5353, 0, index) for index, name in socket.if_nameindex() if name != "lo"]))
    except OSError:
        pass
    try:
        s4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s4.bind(("0.0.0.0", 0))
        out.append((s4, [("224.0.0.251", 5353)]))
    except OSError:
        pass
    return out


def _ask(packet, timeout):
    socks = _sockets()
    records = []
    try:
        for sock, targets in socks:
            for target in targets:
                try:
                    sock.sendto(packet, target)
                except OSError:
                    pass
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            ready, _, _ = select.select([s for s, _ in socks], [], [], left)
            for sock in ready:
                try:
                    data, _ = sock.recvfrom(9000)
                    records.extend(parse_records(data))
                except (OSError, ValueError, struct.error, IndexError):
                    pass
    finally:
        for sock, _ in socks:
            sock.close()
    return records


def address_order(address):
    """Global IPv6 first, then ULA, then IPv4; link-local last (needs a zone)."""
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        return 3 if ip.is_link_local else (1 if ip.is_private else 0)
    return 2


def discover(timeout=1.5):
    """{hostname: [addresses, best first]} for every backpack that answers."""
    records = _ask(_query((SERVICE, 12)), timeout)
    instances = {value for _, kind, value in records if kind == "PTR"}
    targets = {name: value for name, kind, value in records if kind == "SRV"}
    missing = [i for i in instances if i not in targets]
    if missing:
        records += _ask(_query(*[(i, 33) for i in missing]), timeout)
        targets.update({name: value for name, kind, value in records if kind == "SRV"})
    hosts = {t for t in targets.values() if t.split(".")[0].startswith(HOSTNAME_PREFIX)}
    found = {}
    for host in hosts:
        addresses = {v for n, k, v in records if n == host and k in ("A", "AAAA")}
        if not addresses:
            addresses = {v for n, k, v in _ask(_query((host, 28), (host, 1)), timeout)
                         if n == host and k in ("A", "AAAA")}
        usable = sorted((a for a in addresses if address_order(a) < 3), key=address_order)
        if usable:
            found[host.split(".")[0]] = usable
    return found
