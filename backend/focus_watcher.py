#!/usr/bin/python3
"""Hide the dropdown terminal as soon as it loses focus.

The dropdown lives on `special:dropdown`. With `input:special_fallthrough`
enabled (set by this plugin's Hyprland rules) clicking a window outside the
dropdown focuses that window, so the terminal is no longer active and this
watcher hides it again.

Event-driven, not a poll: it subscribes to Hyprland's event socket
(`$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock`) and reacts
to `activewindow>>CLASS,TITLE` and `activespecial>>NAME,MONITOR`. The previous
implementation asked `hyprctl activewindow -j` five times a second - ~432k
process spawns and compositor round trips a day - to observe something the
compositor already announces as an event. Idle cost is now zero wakeups.

It also publishes the state the bar widget needs to
`$XDG_RUNTIME_DIR/dropdown-terminal.state` (0600, written atomically), which
replaces the widget's old five-second `omarchy-dropdown-terminal status` chain.

Every path and tool it touches is validated first (see backend/proc.py): tools
come from a fixed root-owned candidate list and never from PATH, children run
with a minimal environment, captured output is bounded, and the socket and
runtime directory must be owned by this user (a planted socket must not be able
to forge focus events). If anything fails validation the watcher exits non-zero
instead of proceeding; Panel.qml restarts it on a 10 s watchdog.

Started by Panel.qml. An exclusive, `O_NOFOLLOW` 0600 flock keeps a stale
instance from racing a fresh one - the hide path toggles, so two watchers
reacting to the same focus change could re-open what the first one closed.
"""

import fcntl
import json
import os
import re
import selectors
import signal
import socket
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import proc  # noqa: E402  (sibling module: trusted plugin code)

APP_ID = "org.omarchy.dropdown-terminal"
SPECIAL_WS = "special:dropdown"
STARTUP_GRACE = 0.5      # let a shell reload settle before acting
POLL_INTERVAL = 2.0      # degraded mode only, when there is no event socket
MAX_LINE = 4096          # bytes; longer event lines are dropped, not buffered
MAX_BUFFER = 65536       # bytes of unterminated event data we will hold
HIDE_TIMEOUT = 8.0
SIGNATURE_RE = re.compile(r"\A[A-Za-z0-9_]{1,128}\Z")

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CLI = os.path.join(PLUGIN_ROOT, "bin", "omarchy-dropdown-terminal")
STATE_NAME = "dropdown-terminal.state"
LOCK_NAME = "dropdown-terminal-watch.lock"


def log(message):
    print(f"focus_watcher: {message}", file=sys.stderr, flush=True)


class Watcher:
    """Focus/visibility state machine driven by Hyprland event lines.

    Pure and side-effect free so the tests can feed it lines directly. `feed()`
    returns the action to take: None, "state" (visibility changed: persist it)
    or "hide" (the dropdown just lost focus while visible).
    """

    def __init__(self, app_id=APP_ID, workspace=SPECIAL_WS):
        self.app_id = app_id
        self.workspace = workspace
        self.focused = False
        self.visible = False

    def feed(self, raw):
        if isinstance(raw, bytes):
            if len(raw) > MAX_LINE:
                return None
            raw = raw.decode("utf-8", "replace")
        if not raw or len(raw) > MAX_LINE:
            return None
        line = raw.rstrip("\r\n")
        if ">>" not in line:
            return None
        event, payload = line.split(">>", 1)

        if event == "activewindow":
            cls = payload.split(",", 1)[0]
            if cls == self.app_id:
                self.focused = True
                return None
            was_focused = self.focused
            self.focused = False
            if was_focused and self.visible:
                return "hide"
            return None

        if event in ("activespecial", "activespecialv2"):
            # "NAME,MONITOR" for activespecial; the v2 form prefixes the id:
            # "-98,special:dropdown,HDMI-A-1", so the name is the second field.
            fields = payload.split(",")
            name = fields[1] if (event == "activespecialv2" and len(fields) > 1) else fields[0]
            if name == self.workspace:
                self.visible = True
                return "state"
            self.visible = False
            return "state"

        return None


# ------------------------------------------------------------ validated paths


def runtime_dir():
    """The runtime directory, or None when nothing safe is available.

    Only an absolute, existing directory owned by this user and inaccessible to
    group/others is accepted, so a spoofed XDG_RUNTIME_DIR cannot redirect the
    state file or the socket path outside the session runtime directory.
    """
    for candidate in (os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}"):
        if not candidate or not os.path.isabs(candidate):
            continue
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if st.st_uid != os.getuid():
            continue
        if st.st_mode & 0o077:
            continue
        return candidate
    return None


def socket_path(rt):
    """The Hyprland event socket, or None if it cannot be trusted.

    The instance signature comes from the environment, so it is validated
    against a strict charset before being joined into a path (a `..` in it
    would otherwise walk out of the runtime directory), and the socket itself
    must be a socket owned by this user - a planted socket could forge
    activewindow events and drive the hide path.
    """
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or ""
    if not SIGNATURE_RE.match(signature):
        return None
    path = os.path.join(rt, "hypr", signature, ".socket2.sock")
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return None
    return path


def write_state(path, visible):
    """Publish visibility atomically at 0600 (mkstemp + replace, no symlink edge)."""
    directory = os.path.dirname(path)
    payload = json.dumps({"visible": bool(visible), "updated": int(time.time())}) + "\n"
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ddterm-state-")
    except OSError as exc:
        log(f"cannot create state file in {directory}: {exc}")
        return False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log(f"cannot write state file {path}: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


# ------------------------------------------------------------------ actions


def hide_dropdown():
    """Hide through the plugin CLI, which owns the guarded Lua dispatch."""
    for attempt in (1, 2):
        try:
            result = proc.run([CLI, "close"], timeout=HIDE_TIMEOUT)
        except (ValueError, OSError, proc.ToolNotFound) as exc:
            log(f"cannot run the plugin CLI: {exc}")
            return False
        if result.returncode == 0:
            return True
        log(f"close attempt {attempt} failed (rc={result.returncode})")
        time.sleep(0.2)
    return False


def is_visible(hyprctl):
    result = proc.run([hyprctl, "monitors", "-j"], timeout=3)
    if result.returncode != 0:
        return False
    try:
        for monitor in json.loads(result.stdout):
            name = ((monitor.get("specialWorkspace") or {}).get("name")) or ""
            if name == SPECIAL_WS:
                return True
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False
    return False


def active_class(hyprctl):
    result = proc.run([hyprctl, "activewindow", "-j"], timeout=3)
    if result.returncode != 0:
        return ""
    try:
        return json.loads(result.stdout).get("class") or ""
    except (json.JSONDecodeError, AttributeError, TypeError):
        return ""


# -------------------------------------------------------------------- loops


def handle(watcher, state_path, action):
    if action == "state":
        write_state(state_path, watcher.visible)
    elif action == "hide":
        log("focus left the dropdown; hiding")
        hide_dropdown()


def event_loop(watcher, state_path, path, hyprctl):
    """Read Hyprland events until the socket dies; reconnect with backoff."""
    backoff = 0.5
    while True:
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(1.0)
            connection.connect(path)
        except OSError as exc:
            log(f"event socket unusable ({exc}); retrying in {backoff:.1f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)
            if backoff >= 10.0 and socket_path(runtime_dir() or "") is None:
                log("no event socket found; falling back to polling")
                return poll_loop(watcher, state_path, hyprctl)
            continue
        backoff = 0.5
        log("subscribed to the Hyprland event socket")
        buffer = b""
        try:
            while True:
                try:
                    chunk = connection.recv(8192)
                except socket.timeout:
                    continue
                except OSError as exc:
                    log(f"event socket read failed: {exc}")
                    break
                if not chunk:
                    log("event socket closed by the compositor")
                    break
                buffer += chunk
                if len(buffer) > MAX_BUFFER:
                    log("event buffer overflow; resynchronising")
                    buffer = b""
                    continue
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    handle(watcher, state_path, watcher.feed(raw))
        finally:
            try:
                connection.close()
            except OSError:
                pass
        time.sleep(0.5)


def poll_loop(watcher, state_path, hyprctl):
    """Degraded mode for an engine without the event socket.

    Slower (2 s, not 5 Hz) and reaped per call, but it keeps click-to-dismiss
    working. Visibility is still authoritative from the compositor.
    """
    log("polling the compositor every 2s (degraded mode)")
    while True:
        handle(watcher, state_path, watcher.feed(f"activespecial>>{SPECIAL_WS if is_visible(hyprctl) else ''},-"))
        handle(watcher, state_path, watcher.feed(f"activewindow>>{active_class(hyprctl)},"))
        time.sleep(POLL_INTERVAL)


def acquire_lock(rt):
    """Exclusive single-instance lock; raises OSError when it cannot be taken.

    O_NOFOLLOW (a symlinked lock path must not be followed - it would let a
    planted link truncate or lock an arbitrary user file), O_CLOEXEC (so a
    spawned child cannot hold the lock), 0600.
    """
    fd = os.open(
        os.path.join(rt, LOCK_NAME),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def main():
    rt = runtime_dir()
    if rt is None:
        log("no safe runtime directory (need an absolute, uid-owned, 0700 dir)")
        return 2

    # Single instance, fail closed: a second watcher reacting to the same focus
    # change could undo the first one's hide (the dispatch toggles). The
    # Panel.qml watchdog retries, so a transient failure self-heals.
    try:
        acquire_lock(rt)  # held for the lifetime of this process
    except OSError as exc:
        log(f"cannot take the single-instance lock: {exc}")
        return 2

    try:
        hyprctl = proc.tool("hyprctl")
    except proc.ToolNotFound as exc:
        log(str(exc))
        return 2

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))

    state_path = os.path.join(rt, STATE_NAME)
    watcher = Watcher()
    watcher.visible = is_visible(hyprctl)
    write_state(state_path, watcher.visible)

    time.sleep(STARTUP_GRACE)

    path = socket_path(rt)
    if path is None:
        return poll_loop(watcher, state_path, hyprctl)
    return event_loop(watcher, state_path, path, hyprctl)


if __name__ == "__main__":
    sys.exit(main())
