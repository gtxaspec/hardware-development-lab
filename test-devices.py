#!/usr/bin/env python3
"""The device map: validation, import, references kept consistent, bench.py.

Runs the panel against a temp config with fake boards; no hardware.
"""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from uart_stream import UartHub

spec = importlib.util.spec_from_file_location("relay_panel", Path(__file__).with_name("relay-panel.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

CONFIG = {
    "boards": [{"address": "0x20", "bus": 2, "size": 16, "name": "Power"},
               {"address": "0x27", "bus": 2, "size": 16, "name": "Boot"},
               {"address": "0x21", "bus": 2, "size": 8, "name": "Extra"}],
    "gpios": [{"wpi": 27, "name": "Mux"}],
    "backpacks": [{"id": 1, "name": "bp", "host": "192.0.2.1", "mdns": "thingino-backpack-1a2b"}],
}


class PanelFixture(unittest.TestCase):
    """The server on a temp config with fake boards; no tests of its own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-panel-device-test-")
        self.addCleanup(self.temp.cleanup)
        self.config_file = Path(self.temp.name) / "config.json"
        self.config_file.write_text(json.dumps(CONFIG))
        hub = UartHub(count=24)
        self.addCleanup(hub.close)
        for attribute, value in [("CONFIG_FILE", self.config_file), ("uart_hub", hub),
                                 ("TOKENS_FILE", Path(self.temp.name) / "tokens.json")]:
            patcher = patch.object(rp, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = rp.ThreadingHTTPServer(("127.0.0.1", 0), rp.RelayHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def devices(self):
        return {d["name"]: d for d in self.call("GET", "/api/devices")[1]["devices"]}


class DeviceTests(PanelFixture):
    def test_validation(self):
        ok = {"name": "t31x", "power": "B0/10", "boot": {"board": 1, "relay": 8}, "uart": "ttyUSB0",
              "usb_mux": {"wpi": 27}, "settle_secs": 4, "notes": "Z55"}
        self.assertEqual(self.call("POST", "/api/devices", ok)[0], 200)
        d = self.devices()["t31x"]
        self.assertEqual((d["power"], d["boot"], d["uart"], d["uart_name"], d["usb_mux"]),
                         ({"board": 0, "relay": 10}, {"board": 1, "relay": 8}, 0, "ttyUSB0",
                          {"wpi": 27, "host_state": "off"}))
        for bad, why in [({"name": "T31X", "power": "B0/1"}, "already called"),
                         ({"name": "x", "power": "B0/10"}, "already belongs to 't31x'"),
                         ({"name": "x", "power": "B5/0"}, "does not exist"),
                         ({"name": "x", "power": "B2/8"}, "has no relay 8"),
                         ({"name": "x", "boot": "B1/0"}, "needs a power relay or a backpack"),
                         ({"name": "x", "power": "B0/1", "usb_mux": 3}, "not configured"),
                         ({"name": "x", "backpack": "bp", "power": "B0/1"}, "no relays or mux"),
                         ({"name": "x", "backpack": "nope"}, "no backpack"),
                         ({"name": "x", "power": "B0/1", "settle_secs": 0}, "between 0 and 600"),
                         ({"name": "", "power": "B0/1"}, "name is required")]:
            status, body = self.call("POST", "/api/devices", bad)
            self.assertEqual(status, 400, bad)
            self.assertIn(why, body["error"], bad)
        self.assertEqual(self.call("POST", "/api/devices", {"name": "cam", "backpack": "thingino-backpack-1a2b"})[0], 200)
        status, body = self.call("POST", "/api/devices", {"name": "cam2", "backpack": 1})
        self.assertIn("already 'cam'", body["error"])
        self.assertEqual(self.devices()["cam"]["uart"], rp.backpack_uart(1))

    def test_partial_update_keeps_and_clears_fields(self):
        self.call("POST", "/api/devices", {"name": "t40", "power": "B0/12", "boot": "B1/9", "uart": 12})
        uid = self.devices()["t40"]["id"]
        self.assertEqual(self.call("PUT", "/api/device", {"id": uid, "notes": "NAND"})[0], 200)
        d = self.devices()["t40"]
        self.assertEqual((d["boot"], d["uart"], d["notes"]), ({"board": 1, "relay": 9}, 12, "NAND"))
        self.call("PUT", "/api/device", {"id": uid, "uart": None})
        self.assertNotIn("uart", self.devices()["t40"])

    def test_import_is_all_or_nothing_and_upserts_by_name(self):
        self.call("POST", "/api/devices", {"name": "a", "power": "B0/1"})
        status, body = self.call("POST", "/api/devices/import", {"devices": [
            {"name": "A", "power": "B0/1", "notes": "updated"}, {"name": "b", "power": "B0/2"},
            {"name": "c", "power": "B0/2"}]})
        self.assertEqual(status, 400)
        self.assertEqual(set(self.devices()), {"a"}, "a refused import changes nothing")
        status, body = self.call("POST", "/api/devices/import", {"devices": [
            {"name": "A", "power": "B0/1", "notes": "updated"}, {"name": "b", "power": "B0/2"}]})
        self.assertEqual([r["result"] for r in body["devices"]], ["updated", "added"])
        # Matched regardless of case; the pushed spelling is the one kept.
        self.assertEqual(self.devices()["A"]["notes"], "updated")

    def test_ids_are_never_reused(self):
        self.call("POST", "/api/devices", {"name": "a", "power": "B0/1"})
        uid = self.devices()["a"]["id"]
        self.assertEqual(self.call("DELETE", f"/api/devices/{uid}")[0], 200)
        self.call("POST", "/api/devices", {"name": "b", "power": "B0/1"})
        self.assertGreater(self.devices()["b"]["id"], uid)

    def test_what_a_device_uses_cannot_vanish_under_it(self):
        self.call("POST", "/api/devices", {"name": "late", "power": "B2/3", "usb_mux": 27})
        self.call("POST", "/api/devices", {"name": "bpcam", "backpack": 1})
        self.assertEqual(self.call("DELETE", "/api/boards/2")[0], 409)
        self.assertEqual(self.call("DELETE", "/api/gpios/27")[0], 409)
        self.assertEqual(self.call("DELETE", "/api/backpacks/1")[0], 409)
        status, body = self.call("PUT", "/api/board/settings", {"board": 2, "size": 2})
        self.assertEqual(status, 409)
        # Removing an earlier, unused board moves later references down with it.
        self.assertEqual(self.call("DELETE", "/api/boards/0")[0], 200)
        self.assertEqual(self.devices()["late"]["power"], {"board": 1, "relay": 3})

    def test_bench_py_reads_and_pushes_the_map(self):
        toml = Path(self.temp.name) / "bench.toml"
        toml.write_text(f'panel_url = "http://127.0.0.1:{self.server.server_port}"\n'
                        '[devices.t41nq]\npower = { board = 0, relay = 13 }\nboot = { board = 1, relay = 12 }\n'
                        'uart = "ttyUSB9"\n')
        run = lambda *a: subprocess.run([sys.executable, str(Path(__file__).with_name("tools") / "bench.py"), *a],
                                        env=dict(os.environ, BENCH_TOML=str(toml)), capture_output=True, text=True)
        listed = run("list")
        self.assertIn("run `bench.py push`", listed.stderr.lower().replace("run `bench.py push`", "run `bench.py push`"))
        self.assertEqual(run("push").returncode, 0)
        shown = json.loads(run("show", "t41nq").stdout)
        self.assertEqual((shown["power"], shown["uart"], shown["settle_secs"]),
                         ({"board": 0, "relay": 13}, "ttyUSB9", 3))
        self.assertIn("t41nq", run("list").stdout)
        self.assertEqual(run("show", "nope").returncode, 1)



class ClaimTests(PanelFixture):
    """Claims on top of the same server and fake bench (relay writes are stubbed)."""

    def setUp(self):
        super().setUp()
        for name in ("set_relay", "toggle_relay", "set_all", "i2c_init_board", "gpio_write", "gpio_toggle_pin"):
            patcher = patch.object(rp, name, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(rp, "CLAIMS_FILE", Path(self.temp.name) / "claims.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.call("POST", "/api/devices", {"name": "t31x", "power": "B0/10", "boot": "B1/8", "uart": 0,
                                           "usb_mux": 27})

    def as_(self, holder, method, path, body=None, force=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/json"}
        if holder:
            headers.update({"X-Bench-Holder": holder, "X-Bench-Holder-Label": f"{holder} label"})
        if force:
            headers["X-Bench-Force"] = "1"
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def claim(self):
        return {c["device"]: c for c in self.call("GET", "/api/claims")[1]["claims"]}

    def test_claim_blocks_others_and_force_overrides(self):
        status, body = self.as_("alice", "POST", "/api/claims", {"device": "t31x", "note": "flashing", "duration": "30m"})
        self.assertEqual(status, 200, body)
        c = self.claim()["t31x"]
        self.assertEqual((c["holder"], c["label"], c["note"]), ("alice", "alice label", "flashing"))
        self.assertTrue(1700 < c["remaining_s"] <= 1800)
        relay = {"board": 0, "relay": 10, "state": True}
        self.assertEqual(self.as_("alice", "POST", "/api/relay/set", relay)[0], 200)
        status, body = self.as_("bob", "POST", "/api/relay/set", relay)
        self.assertEqual(status, 423)
        self.assertIn("t31x is in use by alice label", body["error"])
        self.assertIn("(flashing)", body["error"])
        self.assertEqual(self.as_(None, "POST", "/api/relay/set", relay)[0], 423, "no holder is not the holder")
        self.assertEqual(self.as_("bob", "POST", "/api/relay/set", relay, force=True)[0], 200)
        self.assertEqual(self.as_("bob", "POST", "/api/relay/set", dict(relay, force=True))[0], 200)
        # Every way to switch the device is covered; unrelated relays are not.
        for path, body in [("/api/relay/toggle", {"board": 1, "relay": 8}), ("/api/board/all", {"board": 0, "state": False}),
                           ("/api/board/reset", {"board": 1}), ("/api/gpio/set", {"wpi": 27, "state": True}),
                           ("/api/gpio/toggle", {"wpi": 27})]:
            self.assertEqual(self.as_("bob", "POST", path, body)[0], 423, path)
        self.assertEqual(self.as_("bob", "POST", "/api/relay/set", {"board": 0, "relay": 3, "state": True})[0], 200)
        self.assertEqual(self.as_("bob", "PUT", "/api/device", {"id": 1, "notes": "x"})[0], 423)
        self.assertEqual(self.as_("bob", "POST", "/api/claims", {"device": "t31x"})[0], 423)
        self.assertEqual(self.as_("bob", "DELETE", "/api/claims/t31x")[0], 423)

    def test_own_actions_renew_the_lease_and_release_frees(self):
        self.as_("alice", "POST", "/api/claims", {"device": "t31x", "duration": "10m"})
        claims = json.loads((Path(self.temp.name) / "claims.json").read_text())
        claims["claims"]["1"]["expires"] -= 400
        (Path(self.temp.name) / "claims.json").write_text(json.dumps(claims))
        self.assertLess(self.claim()["t31x"]["remaining_s"], 300)
        self.as_("alice", "POST", "/api/relay/set", {"board": 0, "relay": 10, "state": False})
        self.assertGreater(self.claim()["t31x"]["remaining_s"], 590, "using the device renews it")
        self.assertEqual(self.as_("alice", "DELETE", "/api/claims/1")[0], 200)
        self.assertEqual(self.claim(), {})
        self.assertEqual(self.as_("bob", "POST", "/api/relay/set", {"board": 0, "relay": 10, "state": True})[0], 200)

    def test_expiry_takeover_and_limits(self):
        self.as_("alice", "POST", "/api/claims", {"device": "t31x"})
        self.assertTrue(7100 < self.claim()["t31x"]["remaining_s"] <= 7200, "default is 2 h")
        self.as_("alice", "POST", "/api/claims", {"device": "t31x", "duration": "99h"})
        self.assertLessEqual(self.claim()["t31x"]["remaining_s"], 24 * 3600, "capped at 24 h")
        claims = json.loads((Path(self.temp.name) / "claims.json").read_text())
        claims["claims"]["1"]["expires"] = 1
        (Path(self.temp.name) / "claims.json").write_text(json.dumps(claims))
        self.assertEqual(self.claim(), {}, "an expired claim is no claim")
        self.assertEqual(self.as_("bob", "POST", "/api/relay/set", {"board": 0, "relay": 10, "state": True})[0], 200)
        self.as_("alice", "POST", "/api/claims", {"device": "t31x", "note": "soak"})
        status, body = self.as_("bob", "POST", "/api/claims", {"device": "t31x", "note": "mine now"}, force=True)
        self.assertIn("took t31x over from alice label", body["message"])
        self.assertEqual(self.claim()["t31x"]["holder"], "bob")
        self.assertEqual(self.as_(None, "POST", "/api/claims", {"device": "t31x"}, force=True)[0], 400,
                         "a claim needs a holder")
        self.assertEqual(self.as_("bob", "POST", "/api/claims", {"device": "t31x", "duration": "soon"})[0], 400)

    def test_console_input_and_backpacks_are_covered(self):
        self.call("POST", "/api/devices", {"name": "bpcam", "backpack": 1})
        self.as_("alice", "POST", "/api/claims", {"device": "t31x"})
        self.as_("alice", "POST", "/api/claims", {"device": "bpcam"})
        status, body = self.as_("bob", "POST", "/api/uart/input", {"uart": 0, "data": "x"})
        self.assertEqual(status, 423)
        status, body = self.as_("alice", "POST", "/api/uart/input", {"uart": 0, "data": "x"})
        self.assertEqual(status, 409, "the holder gets past the claim (then finds no viewer)")
        with patch.object(rp.backpack_monitor, "unit", return_value=object()):
            status, body = self.as_("bob", "POST", "/api/backpack/action", {"id": 1, "action": "bootrom"})
        self.assertEqual(status, 423)
        self.assertIn("bpcam is in use", body["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
