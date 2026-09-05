# Client side of the container -> host overlay protocol.
# Talks over a Unix domain socket.
# Wire format: newline-delimited JSON.
#   {"action": "warn", "seconds": 5}
#   {"action": "clear_warn"}
#   {"action": "logout"}

import json
import logging
import socket

log = logging.getLogger("overlay_client")


class OverlayClient:
    def __init__(self, socket_path: str, reconnect_delay_s: float = 2.0):
        self.socket_path = socket_path
        self.reconnect_delay_s = reconnect_delay_s
        self._sock = None

    def _ensure_connected(self):
        if self._sock is not None:
            return
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.socket_path)
            self._sock = s
        except OSError as e:
            log.warning("Overlay socket not available (%s); is the host shim running?", e)
            self._sock = None

    def _send(self, payload: dict):
        self._ensure_connected()
        if self._sock is None:
            return
        try:
            line = (json.dumps(payload) + "\n").encode("utf-8")
            self._sock.sendall(line)
        except OSError as e:
            log.warning("Lost overlay socket connection (%s), will reconnect", e)
            self._sock = None

    def warn(self, seconds: int):
        self._send({"action": "warn", "seconds": seconds})

    def clear_warn(self):
        self._send({"action": "clear_warn"})

    def logout(self):
        self._send({"action": "logout"})