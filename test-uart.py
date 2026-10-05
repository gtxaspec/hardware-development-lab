#!/usr/bin/env python3
"""UART fan-out and HTTP streaming regressions using local TCP fixtures only."""

import http.client
import importlib.util
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from uart_stream import UartHub, UartSubscription

spec = importlib.util.spec_from_file_location("relay_panel", Path(__file__).with_name("relay-panel.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def until(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("Condition did not become true")


class RawUart:
    def __init__(self, port=0):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.clients = []
        self.accepted = 0
        self.received = bytearray()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
                client.settimeout(0.01)
                with self.lock:
                    self.clients.append(client)
                    self.accepted += 1
            except socket.timeout:
                pass
            with self.lock:
                for client in self.clients[:]:
                    try:
                        data = client.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        data = b""
                    if data:
                        self.received.extend(data)
                    else:
                        client.close()
                        self.clients.remove(client)

    def send(self, data):
        until(lambda: bool(self.clients))
        with self.lock:
            for client in self.clients:
                client.sendall(data)

    def disconnect(self):
        with self.lock:
            for client in self.clients:
                client.shutdown(socket.SHUT_RDWR)
                client.close()
            self.clients.clear()

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.disconnect()
        self.listener.close()


def next_event(subscription, kind):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        event, data = subscription.events.get(timeout=4)
        if event == kind:
            return data
    raise AssertionError(f"Missing {kind} event")


class HubTests(unittest.TestCase):
    def setUp(self):
        self.raw = RawUart()
        self.hub = UartHub(base_port=self.raw.port, count=1, retry_delay=0.05)
        self.addCleanup(self.raw.close)
        self.addCleanup(self.hub.close)

    def test_on_demand_shared_connection_and_no_serial_writes(self):
        self.assertEqual(self.raw.accepted, 0)
        a = self.hub.subscribe({0})
        b = self.hub.subscribe({0})
        until(lambda: self.raw.accepted == 1)
        self.raw.send(b"boot\n")
        self.assertEqual(next_event(a, "output"), {"uart": 0, "text": "boot\n"})
        self.assertEqual(next_event(b, "output"), {"uart": 0, "text": "boot\n"})
        self.hub.unsubscribe(a)
        self.raw.send(b"still live\n")
        self.assertEqual(next_event(b, "output")["text"], "still live\n")
        self.hub.unsubscribe(b)
        until(lambda: not self.raw.clients)
        self.assertEqual(self.raw.accepted, 1)
        self.assertEqual(self.raw.received, b"")

    def test_fragmented_utf8_and_reconnect(self):
        sub = self.hub.subscribe({0})
        self.raw.send(b"\xe2")
        self.raw.send(b"\x82\xac\n")
        self.assertEqual(next_event(sub, "output")["text"], "€\n")
        self.raw.disconnect()
        until(lambda: self.raw.accepted >= 2)
        self.raw.send(b"after reconnect\n")
        self.assertEqual(next_event(sub, "output")["text"], "after reconnect\n")
        self.hub.unsubscribe(sub)

    def test_invalid_selection_and_viewer_limit(self):
        for indices in [set(), {-1}, {1}]:
            with self.assertRaises(ValueError):
                self.hub.subscribe(indices)
        self.hub.max_viewers = 1
        sub = self.hub.subscribe({0})
        with self.assertRaises(RuntimeError):
            self.hub.subscribe({0})
        self.hub.unsubscribe(sub)

    def test_slow_consumer_is_bounded_and_does_not_block(self):
        sub = UartSubscription({0}, queue_size=1)
        sub.publish("output", {"text": "first"})
        for _ in range(10000):
            sub.publish("output", {"text": "more"})
        self.assertTrue(sub.overflow.is_set())
        self.assertEqual(sub.events.qsize(), 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.raw = RawUart()
        self.hub = UartHub(base_port=self.raw.port, count=1)
        self.addCleanup(self.raw.close)
        self.addCleanup(self.hub.close)
        for attribute, value in [("uart_hub", self.hub), ("UART_COUNT", 1),
                                 ("UART_BASE_PORT", self.raw.port), ("UART_HEARTBEAT", 0.05)]:
            patcher = patch.object(rp, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = rp.ThreadingHTTPServer(("127.0.0.1", 0), rp.RelayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        return connection, connection.getresponse()

    def event(self, response, wanted):
        kind = ""
        for _ in range(100):
            line = response.readline().decode().strip()
            if line.startswith("event:"):
                kind = line[6:].strip()
            if line.startswith("data:") and kind == wanted:
                return json.loads(line[5:])
        raise AssertionError(f"Missing SSE event {wanted}")

    def test_list_and_stream_preserve_frame_boundaries_and_close(self):
        _, response = self.request("/api/uarts")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["uarts"],
                         [{"index": 0, "name": "ttyUSB0", "port": self.raw.port}])
        self.assertEqual(self.raw.accepted, 0, "listing opened a UART")
        connection, response = self.request("/api/uart/stream?uart=all")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(self.event(response, "ready")["uarts"], [0])
        text = 'boot\n\nevent: denied\ndata: {}\n<script>literal</script>\n'
        self.raw.send(text.encode())
        self.assertEqual(self.event(response, "output")["text"], text)
        response.close()
        connection.close()
        until(lambda: not self.hub._subscriptions)
        until(lambda: not self.raw.clients)
        self.assertEqual(self.raw.received, b"")

    def test_invalid_and_unauthorized_requests_do_not_open_uart(self):
        for selection in ["", "-1", "1", "host:22", "0%26host=example.com"]:
            _, response = self.request("/api/uart/stream?uart=" + selection)
            self.assertEqual(response.status, 400)
            response.read()
        with patch.object(rp.RelayHandler, "_token_scope", return_value=(False, None)):
            for path in ["/api/uarts", "/api/uart/stream?uart=all"]:
                _, response = self.request(path)
                self.assertEqual(response.status, 403)
                response.read()
        self.assertEqual(self.raw.accepted, 0)

    def test_scoped_tokens_and_revocation(self):
        allowed = {"uarts": [0]}
        with patch.object(rp.RelayHandler, "_token_scope", side_effect=lambda: (True, allowed)):
            _, response = self.request("/api/uart/stream?uart=all")
            self.assertEqual(self.event(response, "ready")["uarts"], [0])
            allowed["uarts"] = []
            self.assertIn("access changed", self.event(response, "denied")["message"])
            response.close()
            until(lambda: not self.hub._subscriptions)



if __name__ == "__main__":
    unittest.main(verbosity=2)
