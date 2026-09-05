"""
Screen capture for GNOME/Wayland via the xdg-desktop-portal ScreenCast
interface + PipeWire — monitor (full-screen) mode only.

This is the only supported way to capture the screen from a sandboxed
process under GNOME/Mutter. Flow:

  1. Ask the portal to CreateSession
  2. SelectSources (monitor)
  3. Start() — triggers the one-time GNOME "share your screen?" dialog
     and returns a PipeWire node id + fd
  4. Open a PipeWire stream on that node, pull frames as numpy arrays

To avoid re-prompting the user every single run, we request a
"restore token" on first consent and persist it, then pass it back in
on subsequent runs so GNOME can silently restore the same permission
(GNOME 43+ supports this — persist_mode=2).

Requires (host + baked into image):
  - python3-gi (PyGObject) with Gst/GstApp bindings
  - gstreamer1.0-pipewire (the pipewiresrc plugin)
  - D-Bus session socket bind-mounted: /run/user/<uid>/bus
  - XDG_RUNTIME_DIR set correctly inside the container
  - XDG_CURRENT_DESKTOP set (passed through from host)
"""

import os
import time
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib, Gio
from pydbus import SessionBus
import numpy as np

TOKEN_PATH = "/data/state/restore_token.txt"


class PortalCaptureError(RuntimeError):
    pass


class ScreenCapture:
    def __init__(self):
        Gst.init(None)
        self.bus = SessionBus()
        self.portal = self.bus.get(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
        )
        self._session_handle = None
        self._pipeline = None
        self._appsink = None
        self._node_id = None

    # ---- portal negotiation -------------------------------------------------

    def _load_restore_token(self):
        if os.path.exists(TOKEN_PATH):
            with open(TOKEN_PATH, "r") as f:
                return f.read().strip()
        return None

    def _save_restore_token(self, token):
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            f.write(token)

    def negotiate(self, timeout_s: int = 60):
        """
        Runs the CreateSession -> SelectSources -> Start portal handshake.
        On first run this pops GNOME's screen-share consent dialog.
        On subsequent runs, if a restore token was saved, GNOME can skip
        the dialog (user still sees a brief indicator, but no click needed).
        """
        screencast = self.bus.get(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
        )["org.freedesktop.portal.ScreenCast"]

        # 1. CreateSession
        session_token = f"nsfwguard{int(time.time())}"
        print(f"  [portal] CreateSession... (pid={os.getpid()})")
        result = self._call_portal_method(
            screencast.CreateSession,
            {"session_handle_token": GLib.Variant("s", session_token)},
        )
        self._session_handle = result["session_handle"]
        print(f"  [portal] session handle: {self._session_handle}")

        # 2. SelectSources — monitor only (type=1)
        restore_token = self._load_restore_token()
        options = {
            "types": GLib.Variant("u", 1),           # 1 = monitor
            "cursor_mode": GLib.Variant("u", 1),      # 1 = hidden
            "persist_mode": GLib.Variant("u", 2),      # 2 = persist until revoked
        }
        if restore_token:
            options["restore_token"] = GLib.Variant("s", restore_token)

        print(f"  [portal] SelectSources... (restore_token={'present' if restore_token else 'none - first run'})")
        self._call_portal_method(screencast.SelectSources, self._session_handle, options)
        print("  [portal] sources selected")

        # 3. Start — this is what triggers the consent dialog (if needed)
        print(f"  [portal] Start... (timeout={timeout_s}s)")
        result = self._call_portal_method(
            screencast.Start, self._session_handle, "", {}, timeout_s=timeout_s
        )

        streams = result["streams"]
        if not streams:
            raise PortalCaptureError("Portal returned no streams — user likely declined sharing")

        self._node_id = streams[0][0]

        new_token = result.get("restore_token")
        if new_token:
            self._save_restore_token(new_token)

    def _call_portal_method(self, method, *args, timeout_s=30):
        """
        Calls a portal method that returns a Request object path, and waits
        for that Request's org.freedesktop.portal.Request.Response signal.

        IMPORTANT ordering: per the xdg-desktop-portal spec, the Request
        object can already have fired its response (and been destroyed) by
        the time a caller gets around to subscribing to it, if the caller
        subscribes only *after* the method call returns. To avoid that
        race, we precompute the Request's object path ourselves (using our
        own unique bus name + a handle_token we control) and subscribe to
        it BEFORE making the call at all.
        """
        handle_token = f"nsfwguard_{int(time.time() * 1000)}"
        unique_name = self.bus.con.get_unique_name().lstrip(":").replace(".", "_")
        expected_path = f"/org/freedesktop/portal/desktop/request/{unique_name}/{handle_token}"

        # Inject handle_token into the options dict, which is always the
        # last positional arg for these portal methods.
        args = list(args)
        args[-1] = dict(args[-1])
        args[-1]["handle_token"] = GLib.Variant("s", handle_token)

        loop = GLib.MainLoop()
        result = {}

        def on_response(connection, sender_name, object_path, interface_name, signal_name, parameters):
            response_code, results = parameters.unpack()
            result["code"] = response_code
            result["results"] = results
            loop.quit()

        subscription_id = self.bus.con.signal_subscribe(
            "org.freedesktop.portal.Desktop",  # sender
            "org.freedesktop.portal.Request",  # interface
            "Response",                         # member (signal name)
            expected_path,                      # object path
            None,                                # arg0 filter
            0,                                   # Gio.DBusSignalFlags.NONE
            on_response,
        )

        try:
            actual_path = method(*args)
            if actual_path != expected_path:
                raise PortalCaptureError(
                    f"Portal request path mismatch: predicted {expected_path}, "
                    f"got {actual_path}. Portal implementation may not follow "
                    f"the documented handle_token convention."
                )

            GLib.timeout_add_seconds(timeout_s, loop.quit)
            loop.run()
        finally:
            self.bus.con.signal_unsubscribe(subscription_id)

        if "code" not in result:
            raise PortalCaptureError("Timed out waiting for portal response (user did not respond?)")
        if result["code"] != 0:
            raise PortalCaptureError(f"Portal request denied or failed (code={result['code']})")
        return result["results"]

    # ---- pipewire frame pulling ----------------------------------------------

    def _open_pipewire_remote(self):
        """
        Requests a PipeWire connection fd from the portal via
        ScreenCast.OpenPipeWireRemote. This is the documented way for a
        sandboxed client to reach the correct, already permission-scoped
        PipeWire instance without needing the host's raw PipeWire socket
        bind-mounted into the container.
        """
        reply, out_fd_list = self.bus.con.call_with_unix_fd_list_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self._session_handle, {})),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        handle_index = reply.unpack()[0]
        return out_fd_list.steal_fds()[handle_index]

    def start_stream(self):
        """Builds GStreamer pipeline reading from the negotiated PipeWire node."""
        if self._node_id is None:
            raise PortalCaptureError("negotiate() must succeed before start_stream()")

        pw_fd = self._open_pipewire_remote()
        print(f"  [pipewire] got remote fd={pw_fd}")

        pipeline_str = (
            f"pipewiresrc fd={pw_fd} path={self._node_id} ! "
            "videoconvert ! video/x-raw,format=RGB ! "
            "appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true"
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_gst_error)
        bus.connect("message::warning", self._on_gst_warning)

        self._appsink = self._pipeline.get_by_name("sink")

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise PortalCaptureError("GStreamer pipeline failed to enter PLAYING state (see bus errors above)")

    def _on_gst_error(self, bus, message):
        err, debug = message.parse_error()
        print(f"  [gst] ERROR: {err}  ({debug})")

    def _on_gst_warning(self, bus, message):
        warn, debug = message.parse_warning()
        print(f"  [gst] WARNING: {warn}  ({debug})")

    def grab_frame(self, timeout_s: float = 2.0) -> np.ndarray | None:
        """Pulls a single latest frame as an (H, W, 3) uint8 RGB numpy array."""
        sample = self._appsink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
        if sample is None:
            return None

        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w = caps.get_value("width")
        h = caps.get_value("height")

        success, mapinfo = buf.map(Gst.MapFlags.READ)
        if not success:
            return None
        try:
            arr = np.frombuffer(mapinfo.data, dtype=np.uint8)
            arr = arr.reshape((h, w, 3)).copy()
        finally:
            buf.unmap(mapinfo)
        return arr

    def stop(self):
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._session_handle:
            try:
                session = self.bus.get(
                    "org.freedesktop.portal.Desktop", self._session_handle
                )
                session.Close()
            except Exception as e:
                print(f"  [portal] warning: failed to close session cleanly: {e}")