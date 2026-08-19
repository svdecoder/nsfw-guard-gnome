"""
Standalone overlay socket test - Phase 3 sanity check.

Runs directly on the HOST (plain python3, no Docker, no venv needed -
only stdlib) and talks to the GNOME Shell extension's Unix socket
exactly like overlay_client.py does from inside the container. This
lets you verify the extension draws/clears boxes correctly without
needing the full detection pipeline running.

Usage:
    python3 test_overlay.py

Before running: install and enable the extension (see README.md
"Phase 3"), and confirm via `journalctl --user -f -o cat /usr/bin/gnome-shell`
(or just watch for a moment) that it logged:
    nsfw-guard-overlay: listening on /home/<you>/.local/share/nsfw-guard/overlay.sock
"""

import json
import socket
import time
from pathlib import Path

SOCKET_PATH = Path.home() / ".local" / "share" / "nsfw-guard" / "overlay.sock"


def send(sock, payload):
    line = (json.dumps(payload) + "\n").encode("utf-8")
    sock.sendall(line)


def main():
    if not SOCKET_PATH.exists():
        print(f"[FAIL] {SOCKET_PATH} does not exist.")
        print("Is the extension installed and enabled? See README.md 'Phase 3'.")
        return

    print(f"Connecting to {SOCKET_PATH}...")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(SOCKET_PATH))
    print("[OK] connected")

    print("\nSending a box in the top-left corner (300x200 at 100,100)...")
    send(sock, {"action": "show", "boxes": [[100, 100, 300, 200]]})
    print("Check your screen now - you should see a black rectangle.")
    time.sleep(3)

    print("\nSending two boxes (top-left + bottom-right corner)...")
    send(sock, {"action": "show", "boxes": [[100, 100, 300, 200], [1500, 800, 300, 200]]})
    print("Check your screen - the first box should have moved/multiplied to two boxes.")
    time.sleep(3)

    print("\nSending clear...")
    send(sock, {"action": "clear"})
    print("Check your screen - both boxes should be gone.")
    time.sleep(1)

    sock.close()
    print("\nDone. If you saw the boxes appear at the right spots, move, and clear")
    print("correctly, Phase 3's overlay shim is working end-to-end.")


if __name__ == "__main__":
    main()
