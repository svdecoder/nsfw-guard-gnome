// nsfw-guard overlay shim -- logout-only with warning grace period.
//
// Listens on a Unix domain socket at ~/.local/share/nsfw-guard/overlay.sock
// and handles three messages:
//   {"action": "warn", "seconds": 5}  -> full-screen warning + notification
//   {"action": "clear_warn"}            -> dismiss the warning
//   {"action": "logout"}                -> blackout + gnome-session-quit

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
        this._warnActors = [];
        this._warnTimeoutId = 0;
        this._warnSecondsLeft = 0;
        this._listener = null;
        this._connections = new Set();
        this._loggingOut = false;
        this._startListening();
    }

    disable() {
        this._stopListening();
        this._clearBoxes();
        this._dismissWarning();
    }

    _startListening() {
        try {
            const dir = Gio.File.new_for_path(GLib.path_get_dirname(SOCKET_PATH));
            dir.make_directory_with_parents(null);
        } catch (e) {}
        try {
            const f = Gio.File.new_for_path(SOCKET_PATH);
            if (f.query_exists(null))
                f.delete(null);
        } catch (e) {}
        this._listener = new Gio.SocketListener();
        const address = Gio.UnixSocketAddress.new(SOCKET_PATH);
        try {
            this._listener.add_address(
                address, Gio.SocketType.STREAM, Gio.SocketProtocol.DEFAULT, null);
        } catch (e) {
            logError(e, 'nsfw-guard-overlay: failed to bind socket');
            this._listener = null;
            return;
        }
        this._acceptNext();
        log('nsfw-guard-overlay: listening');
    }

    _acceptNext() {
        if (!this._listener) return;
        this._listener.accept_async(null, (listener, result) => {
            let connection;
            try { [connection] = listener.accept_finish(result); } catch (e) { return; }
            this._connections.add(connection);
            this._readLines(connection);
            this._acceptNext();
        });
    }

    _readLines(connection) {
        const input = new Gio.DataInputStream({ base_stream: connection.get_input_stream() });
        const readNext = () => {
            input.read_line_async(GLib.PRIORITY_DEFAULT, null, (stream, result) => {
                let line;
                try { [line] = stream.read_line_finish_utf8(result); } catch (e) {
                    this._connections.delete(connection); return;
                }
                if (line === null) { this._connections.delete(connection); return; }
                this._handleLine(line);
                readNext();
            });
        };
        readNext();
    }

    _handleLine(line) {
        let msg;
        try { msg = JSON.parse(line); } catch (e) { return; }
        if (msg.action === 'logout')
            this._triggerLogout();
        else if (msg.action === 'warn')
            this._showWarning(msg.seconds || 5);
        else if (msg.action === 'clear_warn')
            this._dismissWarning();
    }

    _showWarning(totalSeconds) {
        this._dismissWarning();
        this._warnSecondsLeft = totalSeconds;
        this._warnActors = [];
        for (const monitor of Main.layoutManager.monitors) {
            const backdrop = new St.Widget({
                style: 'background-color: rgba(0, 0, 0, 0.85);',
                x: monitor.x, y: monitor.y,
                width: monitor.width, height: monitor.height,
                reactive: true,
            });
            Main.layoutManager.addChrome(backdrop);
            this._warnActors.push(backdrop);

            const label = new St.Label({
                text: this._getWarningText(),
                style: 'color: #ff4444; font-size: 48px; font-weight: bold;',
                x: monitor.x, y: monitor.y + monitor.height / 3,
                reactive: false,
            });
            Main.layoutManager.addChrome(label);
            this._warnActors.push(label);
        }
        try { Main.notifyError('NSFW CONTENT DETECTED',
            "Close the content within " + totalSeconds + "s or the session will log out."); } catch (_) {}
        this._warnTimeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            this._warnSecondsLeft--;
            if (this._warnSecondsLeft <= 0) { this._warnTimeoutId = 0; return GLib.SOURCE_REMOVE; }
            this._updateWarningLabels();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _getWarningText() {
        return 'NSFW CONTENT DETECTED\n\nSession will log out in ' + this._warnSecondsLeft + 's\n\nClose the content to cancel';
    }

    _updateWarningLabels() {
        const newText = this._getWarningText();
        for (const actor of this._warnActors) {
            if (actor instanceof St.Label) { actor.text = newText; }
        }
    }

    _dismissWarning() {
        if (this._warnTimeoutId) { GLib.source_remove(this._warnTimeoutId); this._warnTimeoutId = 0; }
        this._warnSecondsLeft = 0;
        for (const actor of this._warnActors) { try { actor.destroy(); } catch (_) {} }
        this._warnActors = [];
    }

    _triggerLogout() {
        this._dismissWarning();
        this._showFullScreenBlackout();
        if (this._loggingOut) return;
        this._loggingOut = true;
        try {
            const proc = Gio.Subprocess.new(
                ['gnome-session-quit', '--logout', '--no-prompt'],
                Gio.SubprocessFlags.NONE);
            proc.wait_async(null, (p, result) => {
                try { p.wait_finish(result); } catch (e) { this._loggingOut = false; }
            });
        } catch (e) { this._loggingOut = false; }
    }

    _showFullScreenBlackout() {
        this._clearBoxes();
        for (const monitor of Main.layoutManager.monitors) {
            const box = new St.Widget({
                style: 'background-color: black;',
                x: monitor.x, y: monitor.y,
                width: monitor.width, height: monitor.height,
                reactive: false,
            });
            Main.layoutManager.addChrome(box);
            this._boxActors.push(box);
        }
    }

    _clearBoxes() {
        for (const box of this._boxActors) box.destroy();
        this._boxActors = [];
    }

    _stopListening() {
        if (this._listener) { this._listener.close(); this._listener = null; }
        for (const connection of this._connections) {
            try { connection.close(null); } catch (e) {}
        }
        this._connections.clear();
        try {
            const f = Gio.File.new_for_path(SOCKET_PATH);
            if (f.query_exists(null)) f.delete(null);
        } catch (e) {}
    }
}
