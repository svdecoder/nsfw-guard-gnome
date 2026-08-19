"""
Screen capture for GNOME/Wayland via the xdg-desktop-portal ScreenCast
interface + PipeWire.

This is the only supported way to capture the screen from a sandboxed
process under GNOME/Mutter. Flow:

  1. Ask the portal to CreateSession
  2. SelectSources (screen, not window)
  3. Start() -> triggers the one-time GNOME "share your screen?" dialog
     and returns a PipeWire node id + fd
  4. Open a PipeWire stream on that node, pull frames as numpy arrays

To avoid re-prompting the user every single run, we request a
"restore token" on first consent and persist it, then pass it back in
on subsequent runs so GNOME can silently restore the same permission
(GNOME 43+ supports this - persist_mode=2).

Requires (host + baked into image):
  - python3-gi (PyGObject) with Gst/GstApp bindings
  - gstreamer1.0-pipewire (the pipewiresrc plugin)
  - D-Bus session socket bind-mounted: /run/user/<uid>/bus
  - XDG_RUNTIME_DIR set correctly inside the container
  - XDG_CURRENT_DESKTOP set (passed through from host) so the portal
    routes requests to the GNOME backend
  - NOTE: no PipeWire socket needs to be bind-mounted separately - the
    portal's OpenPipeWireRemote call hands us an already-connected fd
    scoped to exactly the permitted capture session (see start_stream)
"""

import os
import time
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib, Gio
from pydbus import SessionBus
import numpy as np

TOKEN_PATH_TEMPLATE = "/data/state/restore_token_{mode}.txt"


class PortalCaptureError(RuntimeError):
    pass


class ScreenCapture:
    def __init__(self, capture_mode: str = "monitor"):
        """
        capture_mode: "monitor" (default) captures the whole screen -
                      covers everything, but the overlay box necessarily
                      appears in the captured stream too (Shell UI chrome
                      is composited on top of the final frame), which is
                      why the live pipeline needs to periodically hide
                      the box to see behind it ("peek", see
                      state_machine.py).

                      "window" captures a single application window you
                      pick when GNOME's share dialog appears. No
                      self-occlusion, no peeking/hiding, no blink - but
                      only that one window is protected.

                      "hybrid" requests BOTH a window and the monitor in
                      ONE consent dialog (GNOME supports multi-select in
                      the picker - hold Ctrl to pick more than one). The
                      window stream runs as the fast, blink-free primary
                      defense; the monitor stream is pulled far less
                      often as a "check everything else on screen" scan
                      (see state_machine.py's WIDE_CHECK_INTERVAL_S).
                      This does NOT enumerate/scan individual windows -
                      the portal has no such API without a consent
                      dialog per window - the monitor stream is what
                      stands in for "everything else".

                      UNVERIFIED: telling the two returned streams apart
                      relies on the portal's optional per-stream
                      "position"/"size" properties (a window is usually
                      smaller than the monitor) - this isn't guaranteed
                      by the spec to always be present or unambiguous.
                      negotiate() logs both streams' raw properties and
                      which role it assigned each one to - check that
                      log the first time and use HYBRID_WINDOW_STREAM_INDEX
                      (0 or 1) to override if it guessed wrong.
        """
        if capture_mode not in ("monitor", "window", "hybrid"):
            raise ValueError(f"capture_mode must be 'monitor', 'window', or 'hybrid', got {capture_mode!r}")
        self.capture_mode = capture_mode
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
        # hybrid mode only: role -> {"node_id", "offset": (x, y), "appsink"}
        self._streams = {}

    # ---- portal negotiation -------------------------------------------------

    def _load_restore_token(self):
        # Tokens are kept separate per capture_mode - a monitor-mode
        # token isn't valid for a window-mode session and vice versa,
        # so mixing them up would just cause the portal to reject it.
        path = TOKEN_PATH_TEMPLATE.format(mode=self.capture_mode)
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read().strip()
        return None

    def _save_restore_token(self, token):
        path = TOKEN_PATH_TEMPLATE.format(mode=self.capture_mode)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
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

        # 2. SelectSources
        restore_token = self._load_restore_token()
        if self.capture_mode == "monitor":
            source_type = 1
        elif self.capture_mode == "window":
            source_type = 2
        else:  # hybrid - request both, ask GNOME to allow picking 2
            source_type = 3  # 1|2

        options = {
            "types": GLib.Variant("u", source_type),
            "cursor_mode": GLib.Variant("u", 1),  # 1 = hidden
            "persist_mode": GLib.Variant("u", 2),  # 2 = persist until revoked
        }
        if self.capture_mode == "hybrid":
            options["multiple"] = GLib.Variant("b", True)
        if restore_token:
            options["restore_token"] = GLib.Variant("s", restore_token)

        print(f"  [portal] SelectSources... (restore_token={'present' if restore_token else 'none - first run'})")
        if self.capture_mode == "hybrid":
            print("  [portal] hybrid mode: pick a WINDOW *and* the MONITOR in the dialog (Ctrl+click both)")
        self._call_portal_method(screencast.SelectSources, self._session_handle, options)
        print("  [portal] sources selected")

        # 3. Start - this is what triggers the consent dialog (if needed)
        print(f"  [portal] Start... (this is where GNOME's dialog should appear if no valid restore_token, timeout={timeout_s}s)")
        result = self._call_portal_method(
            screencast.Start, self._session_handle, "", {}, timeout_s=timeout_s
        )

        streams = result["streams"]
        if not streams:
            raise PortalCaptureError("Portal returned no streams - user likely declined sharing")

        if self.capture_mode != "hybrid":
            self._node_id = streams[0][0]
        else:
            if len(streams) < 2:
                raise PortalCaptureError(
                    f"hybrid mode needs 2 sources but only got {len(streams)} - "
                    "did you select both a window AND the monitor in the dialog "
                    "(Ctrl+click to select more than one)?"
                )
            self._assign_hybrid_streams(streams[:2])

        new_token = result.get("restore_token")
        if new_token:
            self._save_restore_token(new_token)

    def _assign_hybrid_streams(self, streams):
        """
        Figures out which of the 2 returned streams is the window and
        which is the monitor. See the UNVERIFIED note in __init__'s
        docstring - this relies on the optional "size" property being
        present and the window being the smaller of the two, which is a
        reasonable but not spec-guaranteed heuristic. Logs everything so
        you can verify/override via HYBRID_WINDOW_STREAM_INDEX.
        """
        for i, (node_id, props) in enumerate(streams):
            print(f"  [portal] hybrid stream[{i}]: node_id={node_id} properties={dict(props)}")

        override = os.environ.get("HYBRID_WINDOW_STREAM_INDEX")
        if override:
            window_idx = int(override)
            print(f"  [portal] HYBRID_WINDOW_STREAM_INDEX override -> stream[{window_idx}] = window")
        else:
            def area(props):
                size = props.get("size")
                return size[0] * size[1] if size else None

            areas = [area(props) for _, props in streams]
            if all(a is not None for a in areas):
                window_idx = 0 if areas[0] <= areas[1] else 1
                print(f"  [portal] guessed window=stream[{window_idx}] (by smaller reported size)")
            else:
                window_idx = 0
                print(
                    "  [portal] WARNING: no 'size' property on one or both streams - "
                    "can't verify which is the window. Assuming stream[0]=window, "
                    "stream[1]=monitor. If overlay positioning looks wrong, set "
                    "HYBRID_WINDOW_STREAM_INDEX=0 or 1 explicitly."
                )

        monitor_idx = 1 - window_idx
        for role, idx in (("window", window_idx), ("monitor", monitor_idx)):
            node_id, props = streams[idx]
            position = props.get("position")
            offset = (position[0], position[1]) if position else (0, 0)
            if role == "window" and not position:
                print(
                    "  [portal] WARNING: window stream has no 'position' property - "
                    "assuming offset (0,0). If the window isn't at the top-left of "
                    "the screen, the overlay box WILL be misaligned. This is the "
                    "same class of bug as the earlier monitor-mode box-alignment "
                    "issue - verify with DEBUG_SNAPSHOT_DIR before trusting it."
                )
            self._streams[role] = {"node_id": node_id, "offset": offset, "appsink": None}
            print(f"  [portal] role={role} node_id={node_id} offset={offset}")

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

        NOTE: we can't use pydbus's bus.get() to build the subscription
        here, even pre-call - bus.get() does a synchronous Introspect()
        to build its proxy, which requires the target object to already
        exist. The whole point of subscribing early is that the Request
        object does NOT exist yet at that point. So we drop to the
        underlying Gio.DBusConnection.signal_subscribe() instead, which
        matches on a (sender, interface, member, path) pattern regardless
        of whether anything is registered at that path yet.
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
                # Extremely unlikely given the spec's path-derivation rules,
                # but if it ever happens we'd hang waiting on the wrong
                # object - fail loudly instead.
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

        pipewiresrc's `path=<node_id>` property alone (what we were doing
        before) only resolves if the process already has an ambient local
        PipeWire connection to the right instance - inside this container
        there is none, which is why negotiate() succeeded (node_id=108
        printed fine) but every grab_frame() came back empty instantly:
        pipewiresrc had nothing to connect to at all.

        Uses the low-level Gio.DBusConnection call (not pydbus's proxy)
        because this reply carries a Unix file descriptor, which pydbus's
        high-level interface doesn't handle.
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
        """Builds GStreamer pipeline(s) reading from the negotiated PipeWire node(s)."""
        if self._node_id is None and not self._streams:
            raise PortalCaptureError("negotiate() must succeed before start_stream()")

        pw_fd = self._open_pipewire_remote()
        print(f"  [pipewire] got remote fd={pw_fd}")

        if self.capture_mode != "hybrid":
            self._pipeline = self._build_pipeline(pw_fd, self._node_id, "sink")
            self._appsink = self._pipeline.get_by_name("sink")
            self._start_pipeline(self._pipeline)
            return

        # hybrid: two independent pipelines, one PipeWire fd shared between
        # them (the fd is the connection to the PW context, not tied to a
        # specific node - each pipewiresrc's own "path=" selects its node).
        self._pipelines = {}
        for role, info in self._streams.items():
            sink_name = f"sink_{role}"
            pipeline = self._build_pipeline(pw_fd, info["node_id"], sink_name)
            info["appsink"] = pipeline.get_by_name(sink_name)
            self._pipelines[role] = pipeline
            self._start_pipeline(pipeline)
        print(f"  [pipewire] hybrid: started {list(self._streams.keys())} pipelines")

    def _build_pipeline(self, pw_fd, node_id, sink_name):
        pipeline_str = (
            f"pipewiresrc fd={pw_fd} path={node_id} ! "
            "videoconvert ! video/x-raw,format=RGB ! "
            f"appsink name={sink_name} emit-signals=false sync=false max-buffers=1 drop=true"
        )
        pipeline = Gst.parse_launch(pipeline_str)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_gst_error)
        bus.connect("message::warning", self._on_gst_warning)
        return pipeline

    def _start_pipeline(self, pipeline):
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise PortalCaptureError("GStreamer pipeline failed to enter PLAYING state (see bus errors above)")

    def _on_gst_error(self, bus, message):
        err, debug = message.parse_error()
        print(f"  [gst] ERROR: {err}  ({debug})")

    def _on_gst_warning(self, bus, message):
        warn, debug = message.parse_warning()
        print(f"  [gst] WARNING: {warn}  ({debug})")

    def grab_frame(self, timeout_s: float = 2.0) -> np.ndarray | None:
        """Pulls a single latest frame as an (H, W, 3) uint8 RGB numpy array. Non-hybrid modes only."""
        return self._grab_from_appsink(self._appsink, timeout_s)

    def grab_frame_role(self, role: str, timeout_s: float = 2.0):
        """
        Hybrid mode only. Returns (frame, offset) for the given role
        ("window" or "monitor"), where offset is the (x, y) to add to
        the model's box coordinates to get absolute screen position -
        see _assign_hybrid_streams for how/whether that offset is known.
        """
        info = self._streams[role]
        frame = self._grab_from_appsink(info["appsink"], timeout_s)
        return frame, info["offset"]

    def _grab_from_appsink(self, appsink, timeout_s):
        sample = appsink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
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
        for pipeline in getattr(self, "_pipelines", {}).values():
            pipeline.set_state(Gst.State.NULL)
        if self._session_handle:
            try:
                session = self.bus.get(
                    "org.freedesktop.portal.Desktop", self._session_handle
                )
                session.Close()
            except Exception as e:
                print(f"  [portal] warning: failed to close session cleanly: {e}")
