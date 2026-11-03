"""On-demand fan-out of raw TCP UART consoles (standard library only)."""

import codecs
import queue
import socket
import threading


class UartNotConnected(Exception):
    """Input arrived for a UART with no live upstream connection."""


class UartSubscription:
    def __init__(self, indices, queue_size=128):
        self.indices = frozenset(indices)
        self.events = queue.Queue(maxsize=queue_size)
        self.overflow = threading.Event()

    def publish(self, event, data):
        if self.overflow.is_set():
            return
        try:
            self.events.put_nowait((event, data))
        except queue.Full:
            # A slow browser must never stall a UART or grow memory without bound.
            self.overflow.set()


class UartChannel:
    def __init__(self, host, port, index, retry_delay=2.0, name=None, yield_on=None):
        self.host, self.port, self.index = host, port, index
        self.name = name or f"ttyUSB{index}"
        self.retry_delay = retry_delay
        # A console that serves one client at a time (a backpack) announces a
        # takeover with this text before hanging up. Reconnecting would just
        # take it back, so the channel stays down until a new viewer asks.
        self.yield_on = yield_on
        self._yielded = False
        self._cv = threading.Condition()
        # Held across every send and while the reader drops the socket, so a
        # write can never land on a descriptor number the kernel has reused.
        self._write_lock = threading.Lock()
        self._sock = None
        self._listeners = set()
        self._thread = None
        self._closed = False
        self._status = {"uart": index, "status": "connecting"}

    def add(self, subscription):
        with self._cv:
            self._listeners.add(subscription)
            self._yielded = False
            subscription.publish("status", dict(self._status))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run,
                                                name=f"uart-{self.index}", daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def remove(self, subscription):
        with self._cv:
            self._listeners.discard(subscription)
            self._cv.notify_all()

    def _active(self):
        with self._cv:
            return bool(self._listeners) and not self._closed and not self._yielded

    def _publish(self, event, **data):
        payload = {"uart": self.index, **data}
        with self._cv:
            if event == "status":
                self._status = payload
            for listener in self._listeners:
                listener.publish(event, payload)

    def _run(self):
        while True:
            with self._cv:
                while (not self._listeners or self._yielded) and not self._closed:
                    if not self._yielded:
                        self._status = {"uart": self.index, "status": "connecting"}
                    self._cv.wait()
                if self._closed:
                    return
            self._publish("status", status="connecting")
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                # Nothing is sent but what write() is handed: no telnet
                # negotiation, no line handling.
                with socket.create_connection((self.host, self.port), timeout=3) as source:
                    source.settimeout(0.5)
                    with self._write_lock:
                        self._sock = source
                    try:
                        self._publish("status", status="connected")
                        while self._active():
                            try:
                                chunk = source.recv(4096)
                            except socket.timeout:
                                continue
                            if not chunk:
                                raise ConnectionError("UART connection closed")
                            text = decoder.decode(chunk)
                            if text:
                                self._publish("output", text=text)
                            if self.yield_on and self.yield_on in chunk:
                                with self._cv:
                                    self._yielded = True
                                raise ConnectionError("console taken over by another client")
                    finally:
                        with self._write_lock:
                            self._sock = None
            except OSError as error:
                tail = decoder.decode(b"", final=True)
                if tail:
                    self._publish("output", text=tail)
                with self._cv:
                    yielded = self._yielded
                if yielded:
                    self._publish("status", status="taken_over",
                                  message="Another client took this console; reopen it here to take it back")
                    continue
                if self._active():
                    self._publish("status", status="disconnected",
                                  message=f"{error}; retrying")
            with self._cv:
                if self._listeners and not self._closed:
                    self._cv.wait(timeout=self.retry_delay)

    def write(self, data):
        """Send raw bytes to the UART over the connection viewers share.

        ser2net delivers bytes from any of its clients to the tty, so this
        needs no connection of its own. With no viewer there is no
        connection, and input nobody can see the answer to is refused.
        """
        with self._write_lock:
            if self._sock is None:
                raise UartNotConnected(f"UART {self.index} is not connected")
            self._sock.sendall(data)

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=4)


class UartHub:
    """At most one upstream connection per UART, regardless of browser count.

    Channels 0..count-1 are the fixed ser2net ports; others (backpack
    consoles) come and go with add_channel/remove_channel.
    """

    def __init__(self, host="127.0.0.1", base_port=3000, count=24,
                 max_viewers=32, retry_delay=2.0):
        if not 0 <= count <= 64 or not 1 <= base_port <= 65535 - max(0, count - 1):
            raise ValueError("Invalid UART count or TCP port range")
        self.retry_delay = retry_delay
        self.channels = {i: UartChannel(host, base_port + i, i, retry_delay)
                         for i in range(count)}
        self.max_viewers = max_viewers
        self._lock = threading.Lock()
        self._subscriptions = set()

    def add_channel(self, index, host, port, name, yield_on=None):
        """Add or retarget a console. A changed address takes effect for the
        next connection; viewers of the old one are closed with it."""
        with self._lock:
            old = self.channels.get(index)
            if old and (old.host, old.port, old.name, old.yield_on) == (host, port, name, yield_on):
                return
            self.channels[index] = UartChannel(host, port, index, self.retry_delay,
                                               name=name, yield_on=yield_on)
            if old is None:
                return
            for subscription in [s for s in self._subscriptions if index in s.indices]:
                subscription.overflow.set()
        old.close()

    def remove_channel(self, index):
        with self._lock:
            channel = self.channels.pop(index, None)
            for subscription in [s for s in self._subscriptions if index in s.indices]:
                subscription.overflow.set()
        if channel:
            channel.close()

    def subscribe(self, indices):
        indices = frozenset(indices)
        if not indices or not indices.issubset(self.channels):
            raise ValueError("Invalid UART selection")
        with self._lock:
            if len(self._subscriptions) >= self.max_viewers:
                raise RuntimeError("Too many UART viewers; close another console and retry")
            # Leave room for the initial statuses even at the 64-UART limit.
            subscription = UartSubscription(indices, queue_size=max(128, len(indices) * 4))
            self._subscriptions.add(subscription)
            for index in sorted(indices):
                self.channels[index].add(subscription)
            return subscription

    def write(self, index, data):
        channel = self.channels.get(index)
        if channel is None:
            raise ValueError("Invalid UART")
        channel.write(data)

    def unsubscribe(self, subscription):
        with self._lock:
            self._subscriptions.discard(subscription)
            for index in subscription.indices:
                channel = self.channels.get(index)
                if channel:
                    channel.remove(subscription)

    def close(self):
        for channel in list(self.channels.values()):
            channel.close()
