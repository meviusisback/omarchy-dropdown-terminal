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
compositor already announces as an event. The socket read blocks, so an idle
watcher is not woken at all.

It also publishes the state the bar widget needs to
`$XDG_RUNTIME_DIR/dropdown-terminal.state` (0600, written atomically), which
replaces the widget's old five-second `omarchy-dropdown-terminal status` chain.

Every path and tool it touches is validated first (see backend/proc.py): tools
come from a fixed root-owned candidate list and never from PATH, children run
with a minimal environment, captured output is bounded, and the runtime
directory, the socket path and the socket itself must be owned by this user (a
planted socket must not be able to forge focus events). If anything fails
validation the watcher exits non-zero instead of proceeding; Panel.qml restarts
it on a 10 s watchdog.

Started by Panel.qml through the plugin CLI (`omarchy-dropdown-terminal watcher`),
which resolves the interpreter itself. An exclusive, `O_NOFOLLOW` 0600 flock
keeps a stale instance from racing a fresh one - the hide path toggles, so two
watchers reacting to the same focus change could re-open what the first one
closed.
"""

import fcntl
import json
import os
import pwd
import re
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
POLL_RECHECK_TICKS = 15  # degraded mode: look for the event socket again (~30 s)
MAX_LINE = 4096          # bytes; longer event lines are dropped, not buffered
MAX_BUFFER = 65536       # bytes of unterminated event data we will hold
HIDE_TIMEOUT = 8.0
EVENT_IDLE_TIMEOUT = 60.0  # recv timeout: a silent socket is probed once a minute
LIVENESS_TIMEOUT = 3.0
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
                changed = not self.visible
                self.visible = True
                return "state" if changed else None
            changed = self.visible
            self.visible = False
            return "state" if changed else None

        return None


# ------------------------------------------------------------ validated paths


def _uid_owned_dir(path):
    """A directory this user owns with no group/other access."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != os.getuid():
        return False
    return not st.st_mode & 0o077


def _ancestors_safe(path, ancestor_uids=None):
    """Every directory above `path`: owned by a trusted uid, unwritable by others.

    A directory whose name another user can replace (a world-writable parent) makes
    the validated leaf meaningless. `ancestor_uids` defaults to root plus this user
    and is parameterised only so the tests can run in the dev sandbox, where
    root-owned paths read as uid 65534 (uid 0 is not mapped into the user namespace).
    """
    trusted = (0, os.getuid()) if ancestor_uids is None else tuple(ancestor_uids)
    current = os.path.dirname(os.path.realpath(path))
    while True:
        try:
            st = os.stat(current)
        except OSError:
            return False
        if not stat.S_ISDIR(st.st_mode):
            return False
        if st.st_uid not in trusted:
            return False
        if st.st_mode & 0o022:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def home_dir():
    """The real home directory, validated the way the backend validates it.

    os.path.expanduser("~") returns "/" for an empty HOME - exactly what Panel.qml
    passes when the session has no HOME - which silently disables the
    "not HOME (nor an ancestor)" guard below. Falls back to the passwd entry.
    """
    candidates = [os.environ.get("HOME", "")]
    try:
        candidates.append(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:  # pragma: no cover
        pass
    for candidate in candidates:
        if not candidate or not os.path.isabs(candidate):
            continue
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            return os.path.realpath(candidate)
    return None


def runtime_dir(ancestor_uids=None):
    """The session runtime directory, or None when nothing acceptable exists.

    Enforced exactly: an absolute, existing directory owned by this user with no
    group/other bits, every ancestor owned by root or this user and not
    group/world-writable, and not HOME (nor an ancestor of HOME). So a spoofed
    XDG_RUNTIME_DIR cannot redirect the state file, the lock or the socket path
    into a directory whose name someone else could swap - and the systemd
    per-user path is accepted as the fallback because it satisfies the same rules.
    This is the single implementation of that rule: the CLI and the widget ask the
    backend for the path instead of repeating it.
    """
    home = home_dir()
    for candidate in (os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}"):
        if not candidate or not os.path.isabs(candidate):
            continue
        real = os.path.realpath(candidate)
        if home is not None and (home == real or home.startswith(real + os.sep)):
            continue  # HOME (or an ancestor of it) is not a runtime directory
        if not _uid_owned_dir(real):
            continue
        if not _ancestors_safe(real, ancestor_uids):
            continue
        return real
    return None


def socket_path(rt):
    """The Hyprland event socket, or None when it cannot be trusted.

    The instance signature comes from the environment, so it is validated against
    a strict charset before being joined into a path (a `..` in it would otherwise
    walk out of the runtime directory); neither the `hypr` directory nor the
    signature directory may be a symlink (lstat, not stat); and the socket itself
    must be a socket owned by this user - a planted socket could forge
    activewindow events and drive the hide path.
    """
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or ""
    if not SIGNATURE_RE.match(signature):
        return None
    hypr = os.path.join(rt, "hypr")
    signature_dir = os.path.join(hypr, signature)
    for directory in (hypr, signature_dir):
        try:
            st = os.lstat(directory)
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):  # lstat: a symlink does not pass
            return None
        if st.st_uid not in (0, os.getuid()):
            return None
    path = os.path.join(signature_dir, ".socket2.sock")
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return None
    return path


def can_connect(path, timeout=1.0):
    """True when the event socket actually accepts a connection right now.

    The socket FILE existing is not enough: a stale socket left behind by a
    restarted compositor keeps os.stat() happy but refuses connections, and
    retrying forever would silently stop hiding the dropdown. Used to decide
    between the event path and the polling fallback.
    """
    connection = None
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(path)
        return True
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()


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
        # fdopen immediately takes ownership of the fd, so no error path can leak it
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
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


def run_tool(argv, timeout=3.0):
    """proc.run with every failure folded into a failed Result (never raises)."""
    try:
        return proc.run(argv, timeout=timeout)
    except (ValueError, OSError, proc.ToolNotFound) as exc:
        log(f"cannot run {argv[0]}: {exc}")
        return proc.Result(1, "", str(exc))


def hide_dropdown():
    """Hide through the plugin CLI, which owns the guarded Lua dispatch."""
    for attempt in (1, 2):
        result = run_tool([CLI, "close"], timeout=HIDE_TIMEOUT)
        if result.returncode == 0:
            return True
        log(f"close attempt {attempt} failed (rc={result.returncode})")
        time.sleep(0.2)
    return False


def is_visible(hyprctl):
    """True/False when the compositor answered, None when the probe failed.

    None must not be folded into False: "no information" and "hidden" lead to
    different actions in the caller (one is a skipped tick, the other can hide the
    terminal out from under the user). Parseable JSON of the wrong shape counts as
    no information too, so it is validated before use.
    """
    result = run_tool([hyprctl, "monitors", "-j"])
    if result.returncode != 0:
        return None
    try:
        monitors = json.loads(result.stdout)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    if not isinstance(monitors, list) or not monitors:
        return None
    if not all(isinstance(monitor, dict) for monitor in monitors):
        return None       # a malformed entry means the payload cannot be trusted
    for monitor in monitors:
        name = ((monitor.get("specialWorkspace") or {}).get("name")) or ""
        if name == SPECIAL_WS:
            return True
    return False


def active_class(hyprctl):
    """The active window's class, or None when the probe failed (see is_visible)."""
    result = run_tool([hyprctl, "activewindow", "-j"])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("class") or ""
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def compositor_alive(hyprctl):
    """One cheap round trip used as a liveness probe for a silent event socket."""
    return run_tool([hyprctl, "monitors", "-j"], timeout=LIVENESS_TIMEOUT).returncode == 0


# -------------------------------------------------------------------- loops


def handle(watcher, state_path, action):
    if action == "state":
        write_state(state_path, watcher.visible)
    elif action == "hide":
        log("focus left the dropdown; hiding")
        hide_dropdown()


def event_loop(watcher, state_path, hyprctl, rt):
    """Subscribe to the event socket until it can no longer be used."""
    backoff = 0.5
    failures = 0
    while True:
        path = socket_path(rt)  # re-validated right before connecting
        if path is None:
            log("no trusted event socket available; leaving the event path")
            return
        connection = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(path)  # blocking: an idle watcher is never woken
        except OSError as exc:
            if connection is not None:
                connection.close()
            failures += 1
            log(f"event socket connect failed ({exc}); attempt {failures}")
            if failures >= 3:
                # A socket file that exists but never accepts (stale socket from a
                # restarted compositor) must not disable dismissal: fall back to
                # polling, which retries the event path periodically.
                log("event socket unusable; falling back to polling")
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
            continue
        backoff = 0.5
        failures = 0  # a successful subscription resets the strike count
        log("subscribed to the Hyprland event socket")
        buffer = b""
        connection.settimeout(EVENT_IDLE_TIMEOUT)
        try:
            while True:
                try:
                    chunk = connection.recv(8192)
                except socket.timeout:
                    # A socket that accepts but never delivers (hung compositor,
                    # half-open connection) must not park the watcher forever: probe
                    # the compositor once a minute and reconnect. Polling cannot help
                    # here - the poller asks the same hyprctl - so this only re-arms
                    # the subscription; main() uses poll_loop when the socket itself
                    # is gone or refuses connections.
                    if not compositor_alive(hyprctl):
                        log("event socket silent and the compositor is not answering")
                        break
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


def poll_tick(watcher, state_path, hyprctl):
    """One degraded-mode tick: probe, then feed ONLY what the probes returned.

    A probe that failed yields None and is skipped entirely: feeding "no
    information" as a fabricated focus change would hide the terminal out from
    under the user mid-keystroke.
    """
    visible = is_visible(hyprctl)
    if visible is not None:
        name = SPECIAL_WS if visible else ""
        handle(watcher, state_path, watcher.feed(f"activespecial>>{name},-"))
    focused = active_class(hyprctl)
    if focused is not None:
        handle(watcher, state_path, watcher.feed(f"activewindow>>{focused},"))


def poll_loop(watcher, state_path, hyprctl, rt):
    """Degraded mode for an engine without the event socket.

    Slower (2 s, not 5 Hz) and reaped per call, but it keeps click-to-dismiss
    working, and it returns to the event path once the socket accepts connections
    again.
    """
    log("polling the compositor every 2s (degraded mode)")
    ticks = 0
    while True:
        poll_tick(watcher, state_path, hyprctl)
        ticks += 1
        if ticks % POLL_RECHECK_TICKS == 0:
            path = socket_path(rt)
            if path is not None and can_connect(path):
                log("event socket is usable again; returning to the event path")
                return
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
        log("no acceptable runtime directory (absolute, uid-owned, 0700, safe ancestors)")
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
    initial = is_visible(hyprctl)
    # A failed first probe must not claim the dropdown is hidden: keep it False (the
    # safe default for "we do not know") and let the loop correct it.
    watcher.visible = initial if initial is not None else False
    write_state(state_path, watcher.visible)

    time.sleep(STARTUP_GRACE)

    while True:
        # Route on whether the socket can actually be subscribed to, not merely on
        # whether it exists: a stale socket file must degrade to polling.
        path = socket_path(rt)
        if path is not None and can_connect(path):
            event_loop(watcher, state_path, hyprctl, rt)
        else:
            poll_loop(watcher, state_path, hyprctl, rt)


if __name__ == "__main__":
    sys.exit(main())
