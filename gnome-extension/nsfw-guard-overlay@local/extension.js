/*
 * nsfw-guard overlay shim (Phase 3).
 *
 * Listens on a Unix domain socket at ~/.local/share/nsfw-guard/overlay.sock
 * and draws/clears black rectangles on the Shell stage based on
 * newline-delimited JSON messages from overlay_client.py (running inside
 * the container):
 *
 *   {"action": "show", "boxes": [[x, y, w, h], ...]}
 *   {"action": "clear"}
 *
 * Intentionally dumb: no image processing, no detection logic, no
 * decision-making. It just draws what it's told, at the coordinates
 * it's given. All of that lives in the container - see HANDOFF.md.
 */

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const SOCKET_PATH = GLib.build_filenamev(
    [GLib.get_home_dir(), '.local', 'share', 'nsfw-guard', 'overlay.sock']);

export default class NsfwGuardOverlayExtension extends Extension {
    enable() {
        this._boxActors = [];
        this._listener = null;
        this._connections = new Set();
        this._loggingOut = false;
        this._startListening();
    }

    disable() {
        this._stopListening();
        this._clearBoxes();
    }

    _startListening() {
        // Parent dir may not exist yet on a fresh install - the container
        // creates it too (state/restore token lives alongside), but the
        // extension can be enabled before the container ever runs.
        try {
            const dir = Gio.File.new_for_path(GLib.path_get_dirname(SOCKET_PATH));
            dir.make_directory_with_parents(null);
        } catch (e) {
            // Already exists - fine.
        }

        // Remove a stale socket file left behind by a crash / unclean
        // shell restart. A leftover file at this path makes add_address()
        // fail with "address already in use" even though nothing is
        // actually listening anymore.
        try {
            const f = Gio.File.new_for_path(SOCKET_PATH);
            if (f.query_exists(null))
                f.delete(null);
        } catch (e) {
            logError(e, 'nsfw-guard-overlay: failed to remove stale socket');
        }

        this._listener = new Gio.SocketListener();
        const address = Gio.UnixSocketAddress.new(SOCKET_PATH);
        try {
            this._listener.add_address(
                address, Gio.SocketType.STREAM, Gio.SocketProtocol.DEFAULT, null);
        } catch (e) {
            logError(e, 'nsfw-guard-overlay: failed to bind overlay socket');
            this._listener = null;
            return;
        }

        this._acceptNext();
        log(`nsfw-guard-overlay: listening on ${SOCKET_PATH}`);
    }

    _acceptNext() {
        if (!this._listener)
            return;
        this._listener.accept_async(null, (listener, result) => {
            let connection;
            try {
                [connection] = listener.accept_finish(result);
            } catch (e) {
                // Listener was closed (disable()) or a transient accept
                // error - either way, stop accepting on this listener.
                return;
            }
            this._connections.add(connection);
            this._readLines(connection);
            this._acceptNext();
        });
    }

    _readLines(connection) {
        const input = new Gio.DataInputStream({
            base_stream: connection.get_input_stream(),
        });

        const readNext = () => {
            input.read_line_async(GLib.PRIORITY_DEFAULT, null, (stream, result) => {
                let line;
                try {
                    [line] = stream.read_line_finish_utf8(result);
                } catch (e) {
                    this._connections.delete(connection);
                    return;
                }

                if (line === null) {
                    // EOF - client (overlay_client.py) closed the connection.
                    this._connections.delete(connection);
                    return;
                }

                this._handleLine(line);
                readNext();
            });
        };

        readNext();
    }

    _handleLine(line) {
        let msg;
        try {
            msg = JSON.parse(line);
        } catch (e) {
            log(`nsfw-guard-overlay: ignoring malformed message: ${line}`);
            return;
        }

        if (msg.action === 'show')
            this._showBoxes(msg.boxes || []);
        else if (msg.action === 'clear')
            this._clearBoxes();
        else if (msg.action === 'logout')
            this._triggerLogout();
    }

    _triggerLogout() {
        // Blackout every monitor immediately - gnome-session-quit is not
        // instantaneous, so this covers the gap between "we decided to
        // log out" and the session actually tearing down.
        this._showFullScreenBlackout();

        // Guard against repeated logout messages (e.g. hybrid mode's
        // per-tick publish) spawning gnome-session-quit more than once
        // while the first request is still being processed.
        if (this._loggingOut)
            return;
        this._loggingOut = true;

        try {
            const proc = Gio.Subprocess.new(
                ['gnome-session-quit', '--logout', '--no-prompt'],
                Gio.SubprocessFlags.NONE);
            proc.wait_async(null, (p, result) => {
                try {
                    p.wait_finish(result);
                } catch (e) {
                    logError(e, 'nsfw-guard-overlay: gnome-session-quit failed');
                    // Session didn't actually end (unusual) - allow a
                    // retry on the next logout message instead of
                    // leaving the machine blacked-out with no way back.
                    this._loggingOut = false;
                }
            });
        } catch (e) {
            logError(e, 'nsfw-guard-overlay: failed to spawn gnome-session-quit');
            this._loggingOut = false;
        }
    }

    _showFullScreenBlackout() {
        this._clearBoxes();
        for (const monitor of Main.layoutManager.monitors) {
            const box = new St.Widget({
                style: 'background-color: black;',
                x: monitor.x,
                y: monitor.y,
                width: monitor.width,
                height: monitor.height,
                reactive: false,
            });
            Main.layoutManager.addChrome(box);
            this._boxActors.push(box);
        }
    }

    _showBoxes(boxes) {
        this._clearBoxes();

        // Detection boxes arrive in physical screen pixel coordinates,
        // matching the raw PipeWire capture frame (see capture.py /
        // test_capture.py, which confirmed 1920x1080 = physical monitor
        // resolution). Shell's own coordinate system is in logical/UI
        // pixels, which differ from physical pixels under fractional
        // scaling (125%, 150%, ...) - divide by the scale factor so
        // boxes land in the right place on scaled displays. At 100%
        // scaling this is a no-op (scale = 1).
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;

        for (const [x, y, w, h] of boxes) {
            const box = new St.Widget({
                style: 'background-color: black;',
                x: x / scale,
                y: y / scale,
                width: w / scale,
                height: h / scale,
                reactive: false,
            });
            Main.layoutManager.addChrome(box);
            this._boxActors.push(box);
        }
    }

    _clearBoxes() {
        for (const box of this._boxActors)
            box.destroy();
        this._boxActors = [];
    }

    _stopListening() {
        if (this._listener) {
            this._listener.close();
            this._listener = null;
        }
        for (const connection of this._connections) {
            try {
                connection.close(null);
            } catch (e) {
                // Already closed - fine.
            }
        }
        this._connections.clear();

        try {
            const f = Gio.File.new_for_path(SOCKET_PATH);
            if (f.query_exists(null))
                f.delete(null);
        } catch (e) {
            // Best-effort cleanup.
        }
    }
}
