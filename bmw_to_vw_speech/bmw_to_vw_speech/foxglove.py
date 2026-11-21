"""Publish ROS topics on the car through its foxglove_bridge, without ROS on the laptop.

Publish-only version of rear_buddy's client (student_ws/src/rear_buddy/rear_buddy/foxglove_client.py):

    bridge = FoxglovePublisher("ws://10.42.0.1:8765").start()
    bridge.advertise("/advertisement", STRING)
    bridge.publish("/advertisement", encode_string("McDonalds"))

It reconnects by itself after Wi-Fi drops and then advertises again.
Protocol: https://github.com/foxglove/ws-protocol/blob/main/docs/spec.md
"""

import json
import struct
import threading

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

# foxglove_bridge 3.x (Foxglove SDK) speaks foxglove.sdk.v1, older bridges foxglove.websocket.v1: offer both.
SUBPROTOCOLS = ["foxglove.sdk.v1", "foxglove.websocket.v1"]
OP_MESSAGE_DATA = 0x01
STRING = "std_msgs/msg/String"
CDR_LE = b"\x00\x01\x00\x00"  # CDR encapsulation header, little endian


def encode_string(text: str) -> bytes:
    """std_msgs/String as CDR: header, uint32 length incl. NUL, UTF-8 bytes, NUL."""
    data = text.encode("utf-8") + b"\x00"
    return CDR_LE + struct.pack("<I", len(data)) + data


class FoxglovePublisher:
    def __init__(self, url: str, log=print, retry_s: float = 2.0):
        self.url, self.log, self.retry_s = url, log, retry_s
        self.channels: dict[str, tuple[int, str]] = {}  # topic -> (our channel id, schema name)
        self.connected = threading.Event()
        self._ws = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="foxglove", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close()
        self._thread.join(timeout=3)

    def advertise(self, topic: str, schema_name: str = STRING):
        with self._lock:
            if topic not in self.channels:
                self.channels[topic] = (len(self.channels) + 1, schema_name)
        if self.connected.is_set():
            self._advertise([topic])

    def publish(self, topic: str, payload: bytes, wait_s: float = 0.0) -> bool:
        """Send one message. False when the bridge is not reachable (within wait_s); the message is dropped."""
        if wait_s and not self.connected.wait(wait_s):
            return False
        return self._send(struct.pack("<BI", OP_MESSAGE_DATA, self.channels[topic][0]) + payload)

    def _advertise(self, topics):
        channels = [{"id": self.channels[t][0], "topic": t, "encoding": "cdr", "schemaName": self.channels[t][1]}
                    for t in topics]
        self._send(json.dumps({"op": "advertise", "channels": channels}))

    def _send(self, data) -> bool:
        with self._lock:
            ws = self._ws
            if ws is None or not self.connected.is_set():
                return False
            try:
                ws.send(data)
                return True
            except (ConnectionClosed, OSError):
                return False

    def _run(self):
        last_error = None
        while not self._stop.is_set():
            try:
                with connect(self.url, subprotocols=SUBPROTOCOLS, max_size=None, open_timeout=5) as ws:
                    with self._lock:
                        self._ws = ws
                    last_error = None
                    for raw in ws:
                        if isinstance(raw, str):
                            self._on_text(json.loads(raw))
                    raise ConnectionError("connection closed")
            except Exception as e:  # noqa: BLE001 - any connection problem: report once and keep retrying
                error = str(e) or type(e).__name__
                if not self._stop.is_set() and error != last_error:
                    self.log(f"🔌 bridge {self.url}: {error}; retrying every {self.retry_s:.0f} s")
                last_error = error
            finally:
                self.connected.clear()
                with self._lock:
                    self._ws = None
            self._stop.wait(self.retry_s)

    def _on_text(self, msg):
        op = msg.get("op")
        if op == "serverInfo":
            self.connected.set()
            self.log(f"🔌 connected to {msg.get('name') or 'bridge'} at {self.url}")
            if "clientPublish" not in msg.get("capabilities", []):
                self.log("⚠️  this bridge does not accept messages from clients (no clientPublish)")
            if self.channels:
                self._advertise(list(self.channels))
        elif op == "status" and msg.get("level", 0) >= 1:
            self.log(f"🔌 bridge says: {msg.get('message')}")
