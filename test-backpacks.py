#!/usr/bin/env python3
"""Backpack support: polling, the action proxy, access, discovery, consoles.

Runs against demo.py's FakeBackpack on local ports; no hardware, no network.
"""

import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import backpacks
import demo
from uart_stream import UartHub

spec = importlib.util.spec_from_file_location("relay_panel", Path(__file__).with_name("relay-panel.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def until(check, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("Condition did not become true")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MdnsTests(unittest.TestCase):
    """The parser against a hand-built answer using name compression."""

    def answer(self):
        def name(text):
            return b"".join(bytes([len(p)]) + p.encode() for p in text.split(".")) + b"\0"

        records = []
        service = name("_thingino._tcp.local")                       # at offset 12
        instance = b"\x16thingino-backpack-1a2b\xc0\x0c"              # pointer to service
        host = b"\x16thingino-backpack-1a2b\x05local\x00"
        records.append(service + struct.pack("!HHIH", 12, 1, 120, len(instance)) + instance)
        inst_off = 12 + len(records[0]) - len(instance)
        srv = struct.pack("!HHH", 0, 0, 80) + host
        records.append(struct.pack("!H", 0xC000 | inst_off) + struct.pack("!HHIH", 33, 1, 120, len(srv)) + srv)
        host_off = 12 + len(records[0]) + len(records[1]) - len(host)
        pointer = struct.pack("!H", 0xC000 | host_off)
        records.append(pointer + struct.pack("!HHIH", 1, 1, 120, 4) + socket.inet_aton("10.0.0.7"))
        v6 = socket.inet_pton(socket.AF_INET6, "fd00::7")
        records.append(pointer + struct.pack("!HHIH", 28, 1, 120, 16) + v6)
        return struct.pack("!6H", 0, 0x8400, 0, 4, 0, 0) + b"".join(records)

    def test_parses_compressed_answer(self):
        records = backpacks.parse_records(self.answer())
        self.assertIn(("_thingino._tcp.local", "PTR", "thingino-backpack-1a2b._thingino._tcp.local"), records)
        self.assertIn(("thingino-backpack-1a2b._thingino._tcp.local", "SRV", "thingino-backpack-1a2b.local"), records)
        self.assertIn(("thingino-backpack-1a2b.local", "A", "10.0.0.7"), records)
        self.assertIn(("thingino-backpack-1a2b.local", "AAAA", "fd00::7"), records)

    def test_discover_keeps_backpacks_only_ipv6_first(self):
        packet = self.answer()
        camera = [("_thingino._tcp.local", "PTR", "ing-wyze-cam3-6e4f._thingino._tcp.local"),
                  ("ing-wyze-cam3-6e4f._thingino._tcp.local", "SRV", "ing-wyze-cam3-6e4f.local"),
                  ("ing-wyze-cam3-6e4f.local", "A", "10.0.0.9")]
        with patch.object(backpacks, "_ask", return_value=backpacks.parse_records(packet) + camera):
            found = backpacks.discover(timeout=0)
        self.assertEqual(found, {"thingino-backpack-1a2b": ["fd00::7", "10.0.0.7"]})

    def test_link_local_is_not_offered(self):
        self.assertEqual(backpacks.address_order("fe80::1"), 3)
        self.assertLess(backpacks.address_order("2001:db8::1"), backpacks.address_order("10.0.0.1"))


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.unit = demo.FakeBackpack("thingino-backpack-test")
        self.addCleanup(self.unit.close)
        self.temp = tempfile.TemporaryDirectory(prefix="relay-panel-backpack-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config_file = base / "config.json"
        self.config_file.write_text(json.dumps({"backpacks": [self.entry(1, self.unit)]}))
        self.hub = UartHub(count=0)
        self.addCleanup(self.hub.close)
        self.monitor = backpacks.BackpackMonitor(on_change=lambda: rp.refresh_backpack_consoles())
        for attribute, value in [("CONFIG_FILE", self.config_file), ("TOKENS_FILE", base / "tokens.json"),
                                 ("uart_hub", self.hub), ("backpack_monitor", self.monitor)]:
            patcher = patch.object(rp, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.monitor.sync([]))
        rp.sync_backpacks(rp.load_config())
        self.server = rp.ThreadingHTTPServer(("127.0.0.1", 0), rp.RelayHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        until(lambda: self.backpack(1)["online"])

    @staticmethod
    def entry(uid, unit):
        return {"id": uid, "name": f"unit {uid}", "host": "127.0.0.1", "mdns": unit.name,
                "http_port": unit.http_port, "console_port": unit.console_port}

    def call(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def backpack(self, uid):
        _, body = self.call("GET", "/api/backpacks")
        return next(b for b in body["backpacks"] if b["id"] == uid)

    def test_state_and_actions_go_through_the_panel(self):
        b = self.backpack(1)
        self.assertEqual((b["power"], b["boot_pin"], b["version"], b["uart"]), ("on", "released", "demo-0.2.0", 101))
        status, body = self.call("POST", "/api/backpack/action", {"id": 1, "action": "bootrom"})
        self.assertEqual((status, body["ok"]), (200, True))
        until(lambda: self.backpack(1)["usb"] == ["a108:c309"])
        _, state = self.call("GET", "/api/state")
        self.assertEqual(state["backpacks"][0]["usb"], ["a108:c309"])
        self.assertEqual(self.call("POST", "/api/backpack/action", {"id": 1, "action": "reboot"})[0], 400)
        self.assertEqual(self.call("POST", "/api/backpack/action", {"id": 7, "action": "bootrom"})[0], 400)
        status, _ = self.call("POST", "/api/backpack/setting", {"id": 1, "usb_port": "sideways"})
        self.assertEqual(status, 400)
        status, _ = self.call("POST", "/api/backpack/setting", {"id": 1, "bootsel": "high"})
        self.assertEqual(status, 200)
        until(lambda: self.backpack(1)["bootsel"] == "high")

    def test_a_unit_that_stops_answering_goes_offline_without_blocking_others(self):
        dead = {"id": 2, "name": "gone", "host": "127.0.0.1", "http_port": free_port()}
        config = rp.load_config()
        config["backpacks"].append(dead)
        rp.save_config(config)
        rp.sync_backpacks(config)
        until(lambda: self.backpack(2)["error"] is not None)
        gone = self.backpack(2)
        self.assertFalse(gone["online"])
        self.assertTrue(self.backpack(1)["online"])
        status, body = self.call("POST", "/api/backpack/action", {"id": 2, "action": "power-on"})
        self.assertEqual(status, 502)

    def test_tokens_need_a_backpacks_grant(self):
        (Path(self.temp.name) / "tokens.json").write_text(json.dumps({"tokens": {
            hashlib.sha256(b"relays-only").hexdigest(): {"allowed": {"relays": []}},
            hashlib.sha256(b"unit-1").hexdigest(): {"allowed": {"backpacks": [1]}}}}))
        body = {"id": 1, "action": "power-cycle"}
        self.assertEqual(self.call("POST", "/api/backpack/action", body, token="relays-only")[0], 403)
        self.assertEqual(self.call("POST", "/api/backpack/action", body, token="unit-1")[0], 200)
        # Adding and removing units reshapes the bench: owner only.
        self.assertEqual(self.call("POST", "/api/backpacks", {"host": "10.0.0.1"}, token="unit-1")[0], 403)
        self.assertEqual(self.call("DELETE", "/api/backpacks/1", token="unit-1")[0], 403)

    def test_removed_units_stay_removed_and_ids_are_never_reused(self):
        status, body = self.call("DELETE", "/api/backpacks/1")
        self.assertEqual(status, 200)
        config = rp.load_config()
        self.assertEqual(config["backpacks"], [])
        self.assertEqual(config["backpacks_ignored"], [self.unit.name])
        found = {self.unit.name: ["127.0.0.1"], "thingino-backpack-new": ["10.0.0.5"]}
        with patch.object(backpacks, "discover", return_value=found):
            rp.discover_backpacks()
        config = rp.load_config()
        self.assertEqual([b["mdns"] for b in config["backpacks"]], ["thingino-backpack-new"])
        self.assertEqual(config["backpacks"][0]["id"], 2, "id 1 was removed and must not come back")
        status, body = self.call("POST", "/api/backpacks", {"host": "10.0.0.6", "name": "manual"})
        self.assertEqual((status, body["id"]), (200, 3))
        self.assertEqual(self.call("POST", "/api/backpacks", {"host": "http://x/y"})[0], 400)

    def test_discovery_follows_a_unit_to_its_new_address(self):
        with patch.object(backpacks, "discover", return_value={self.unit.name: ["127.0.0.1", "10.9.9.9"]}):
            rp.discover_backpacks()
        entry = rp.load_config()["backpacks"][0]
        self.assertEqual((entry["id"], entry["addresses"]), (1, ["127.0.0.1", "10.9.9.9"]))

    def test_console_is_listed_under_the_units_name_and_yields_to_a_takeover(self):
        _, uarts = self.call("GET", "/api/uarts")
        self.assertEqual([(u["index"], u["name"], u["backpack"]) for u in uarts["uarts"]], [(101, "unit 1", 1)])
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/uart/stream?uart=101")
        stream = connection.getresponse()
        seen = []

        def read():
            for raw in stream:
                seen.append(raw.decode())

        threading.Thread(target=read, daemon=True).start()
        until(lambda: any("camera console line" in line for line in seen))
        thief = socket.create_connection(("127.0.0.1", self.unit.console_port))
        self.addCleanup(thief.close)
        until(lambda: any('"taken_over"' in line for line in seen))
        # The panel must not take the console straight back.
        time.sleep(2.5)
        with self.unit.lock:
            self.assertEqual(self.unit.client.getpeername(), thief.getsockname(),
                             "the client that took the console still has it")
        self.assertFalse(any('"connected"' in line for line in seen[[i for i, l in enumerate(seen) if "taken_over" in l][0]:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
