"""
Client side of the container -> host overlay protocol.

Talks over a Unix domain socket (bind-mounted into the container, e.g.
~/.local/share/nsfw-guard/overlay.sock on the host). No TCP/IP, no
network namespace involvement at all - this works fine even with
`docker run --network none` because Unix sockets are just filesystem
objects.

Wire format: newline-delimited JSON, one message per line.
  {"action": "show", "boxes": [[x, y, w, h], ...]}
  {"action": "clear"}
  {"action": "logout"}

The host-side shim (GNOME Shell extension, Phase 3) is intentionally
dumb: it does no image processing, it just draws/clears black
rectangles at the coordinates it's given.
"""

import json
import logging
import socket
import time

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
            return  # degrade silently - detection loop keeps running
        try:
            line = (json.dumps(payload) + "\n").encode("utf-8")
            self._sock.sendall(line)
        except OSError as e:
            log.warning("Lost overlay socket connection (%s), will reconnect", e)
            self._sock = None

    def show(self, detections):
        boxes = [[int(v) for v in d.box] for d in detections]
        self._send({"action": "show", "boxes": boxes})

    def clear(self):
        self._send({"action": "clear"})

    def logout(self):
        self._send({"action": "logout"})
