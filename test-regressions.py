#!/usr/bin/env python3
"""File serving, relay writes and startup regressions, without live hardware.

Run with: python3 test-regressions.py
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(REPO / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = load_module("rp", "relay-panel.py")
demo = load_module("demo", "demo.py")


class StaticFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-panel-static-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.static = self.base / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("panel")
        (self.static / "app.js").write_text("app")
        (self.base / "private.txt").write_text("outside the static directory")
        sibling = self.base / "static-private"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("outside the static directory")
        (self.static / "escape.txt").symlink_to(self.base / "private.txt")
        (self.static / "escape-dir").symlink_to(sibling, target_is_directory=True)
        (self.static / "inside.js").symlink_to(self.static / "app.js")
        patcher = patch.object(rp, "STATIC_DIR", self.static)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, path):
        handler = rp.RelayHandler.__new__(rp.RelayHandler)
        handler.path = path
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET {} HTTP/1.1".format(path)
        handler.wfile = io.BytesIO()
        handler.do_GET()
        headers, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(headers.split()[1]), body

    def test_normal_files_and_internal_symlinks_are_served(self):
        for path, body in (("/", b"panel"), ("/index.html?x=1", b"panel"),
                           ("/app.js", b"app"), ("/inside.js", b"app")):
            with self.subTest(path=path):
                self.assertEqual(self.get(path), (200, body))

    def test_paths_outside_static_are_refused(self):
        for path in ("/../private.txt", "/%2e%2e/private.txt",
                     "/..%2fprivate.txt", "/../static-private/secret.txt",
                     "/escape.txt", "/escape-dir/secret.txt"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"outside the static directory", body)

    def test_missing_and_invalid_paths_are_refused(self):
        for path in ("/missing.txt", "/%00"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)


class RelayWritesTests(unittest.TestCase):
    def test_concurrent_updates_preserve_both_operations(self):
        cases = (
            ("set two relays", lambda: rp.set_relay(2, "0x20", 0, True),
             lambda: rp.set_relay(2, "0x20", 1, True), [3, 0]),
            ("toggle twice", lambda: rp.toggle_relay(2, "0x20", 0),
             lambda: rp.toggle_relay(2, "0x20", 0), [0, 0]),
            ("toggle upper port", lambda: rp.toggle_relay(2, "0x20", 8),
             lambda: rp.toggle_relay(2, "0x20", 9), [0, 3]),
            ("set and toggle", lambda: rp.set_relay(2, "0x20", 0, True),
             lambda: rp.toggle_relay(2, "0x20", 1), [3, 0]),
            ("different ports", lambda: rp.set_relay(2, "0x20", 0, True),
             lambda: rp.set_relay(2, "0x20", 8, True), [1, 1]),
            ("whole board", lambda: rp.set_relay(2, "0x20", 0, True),
             lambda: rp.set_all(2, "0x20", 16, True), [255, 255]),
            ("eight relays", lambda: rp.set_relay(2, "0x20", 0, True),
             lambda: rp.set_all(2, "0x20", 8, True), [255, 0]),
        )
        for name, first_action, second_action, expected in cases:
            with self.subTest(name=name):
                self.check_concurrent(first_action, second_action, expected)

    def check_concurrent(self, first_action, second_action, expected):
        registers = [0, 0]
        first_released = threading.Event()
        second_finished = threading.Event()
        errors = []

        class ScheduledBusLock(rp.BusLock):
            def __exit__(self, *exc):
                result = super().__exit__(*exc)
                # Run the second operation at the first opportunity the bus
                # offers. If a read releases the lock before its write, this
                # deterministically exposes the lost update.
                if threading.current_thread().name == "first" and not first_released.is_set():
                    first_released.set()
                    if not second_finished.wait(3):
                        raise RuntimeError("second operation did not finish")
                return result

        def hardware(cmd, **kwargs):
            if cmd[0] == "i2ctransfer":
                return subprocess.CompletedProcess(cmd, 0,
                    "0x{:02x} 0x{:02x}".format(*registers), "")
            if cmd[0] != "i2cset":
                raise AssertionError("unexpected hardware command: {!r}".format(cmd))
            register = int(cmd[4], 16) - rp.XL_OUTPUT_A
            values = cmd[5:-1] if cmd[-1] == "i" else cmd[5:]
            for offset, value in enumerate(values):
                registers[register + offset] = int(value, 16)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def worker(action, finished=None):
            try:
                action()
            except Exception as error:
                errors.append(error)
            finally:
                if finished is not None:
                    finished.set()

        with patch.object(rp, "i2c_lock", ScheduledBusLock()), \
             patch.object(rp.subprocess, "run", side_effect=hardware):
            first = threading.Thread(target=worker, name="first",
                                     args=(first_action,), daemon=True)
            second = threading.Thread(target=worker, name="second",
                                      args=(second_action, second_finished), daemon=True)
            first.start()
            self.assertTrue(first_released.wait(3), "first operation held the bus indefinitely")
            second.start()
            first.join(4)
            second.join(4)
            self.assertFalse(first.is_alive() or second.is_alive(), "relay operations deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(registers, expected)

    def test_failed_read_never_writes(self):
        failure = subprocess.CompletedProcess([], 1, "", "read failed")
        for action in (rp.set_relay, rp.toggle_relay):
            with self.subTest(action=action.__name__):
                with patch.object(rp.subprocess, "run", return_value=failure) as run:
                    with self.assertRaises(RuntimeError):
                        args = (True,) if action is rp.set_relay else ()
                        action(2, "0x20", 0, *args)
                    self.assertEqual(run.call_count, 1)


class SamplerOrderingTests(unittest.TestCase):
    def test_slow_old_read_cannot_replace_a_newer_observation(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        class Sampler(rp.BenchSampler):
            def sample(self, background=True):
                if threading.current_thread().name == "old-sample":
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("old sample was not released")
                    return {"reading": "old"}
                return {"reading": "new"}

        sampler = Sampler()

        def old_read():
            try:
                sampler.fresh()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=old_read, name="old-sample", daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            written = rp.bump_write_seq()
            sampler.fresh()
            newer = sampler.payload()
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        after = sampler.payload()
        self.assertEqual(after["reading"], "new")
        self.assertEqual(after["sample_seq"], newer["sample_seq"])
        self.assertEqual(after["covers_writes"], written)


class StartupTests(unittest.TestCase):
    def test_startup_preserves_config_without_relay_boards(self):
        for config in (
            {"boards": [], "gpios": [], "pwm": []},
            {"boards": [], "gpios": [{"wpi": 7, "state": True}], "pwm": []},
            {"boards": [], "gpios": [], "pwm": [{"bus": 2, "address": "0x40"}]},
            {"boards": [], "gpios": [{"wpi": 7, "state": True}],
             "pwm": [{"bus": 2, "address": "0x40"}]},
        ):
            with self.subTest(config=config):
                self.check_startup(config)

    def test_new_install_starts_with_no_assumed_hardware(self):
        self.check_startup(None)

    def check_startup(self, config):
        with tempfile.TemporaryDirectory(prefix="relay-panel-startup-test-") as temp:
            path = Path(temp) / "config.json"
            before = json.dumps(config, indent=2) if config is not None else None
            if before is not None:
                path.write_text(before)
            expected = config if config is not None else {"boards": [], "gpios": [], "pwm": []}
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(rp, "CONFIG_FILE", path))
                stack.enter_context(patch.object(rp, "LISTEN_HOST", "127.0.0.1"))
                stack.enter_context(patch.object(rp, "ThreadingHTTPServer", return_value=Mock()))
                initializers = [stack.enter_context(patch.object(rp, name))
                                for name in ("init_boards", "init_gpios", "init_pwm")]
                stack.enter_context(patch.object(rp, "sampler"))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                rp.main()
            for initializer in initializers:
                initializer.assert_called_once_with(expected)
            if before is not None:
                self.assertEqual(path.read_text(), before)
            elif path.exists():
                self.assertEqual(json.loads(path.read_text()), expected)


class DemoTests(unittest.TestCase):
    def test_stub_supports_paired_relay_reads_and_writes(self):
        with tempfile.TemporaryDirectory(prefix="relay-panel-demo-test-") as temp:
            base = Path(temp)
            bin_path, _ = demo.build_sandbox(base)
            self.assertTrue((bin_path / "i2ctransfer").exists())
            with patch.dict(os.environ, {
                "PATH": str(bin_path) + os.pathsep + os.environ.get("PATH", ""),
                "STUB_STATE": str(base / "state.json"),
            }):
                rp.i2c_init_board(2, "0x20")
                rp.set_relay(2, "0x20", 0, True)
                rp.set_relay(2, "0x20", 15, True)
                self.assertEqual(rp.i2c_read_state(2, "0x20"), (1, 128))
                self.assertEqual(rp.i2c_read_pins(2, "0x20"), (1, 128))
                rp.set_all(2, "0x20", 16, True)
                self.assertEqual(rp.i2c_read_state(2, "0x20"), (255, 255))
                rp.set_all(2, "0x20", 8, False)
                self.assertEqual(rp.i2c_read_state(2, "0x20"), (0, 255))
                rp.set_all(2, "0x20", 16, False)
                self.assertEqual(rp.i2c_read_pins(2, "0x20"), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
