#!/usr/bin/env python3
"""
Close the dropdown terminal as soon as it loses focus.

The dropdown lives on `special:dropdown`. With `input:special_fallthrough`
enabled (set by this plugin's Hyprland rules), clicking a window outside the
dropdown focuses that window - so the terminal is no longer the active window.
This watcher notices that and hides the dropdown, returning you to your work.

The hide goes through the plugin's own CLI (`omarchy-dropdown-terminal close`)
instead of a raw `hyprctl dispatch`: on Hyprland 0.56 the legacy
`dispatch togglespecialworkspace <ws>` form is rejected outright ("dispatch in
lua is a shorthand for hl.dispatch(...)"), and the CLI already owns the correct
`hl.dsp.workspace.toggle_special(...)` call plus its guard against toggling an
already-hidden workspace back on.

Started by Panel.qml (`Process { running: true }`, with a watchdog Timer). An
exclusive flock keeps a stale instance from racing a fresh one on the same
toggle.
"""
import fcntl
import json
import os
import subprocess
import sys
import time

APP_ID = "org.omarchy.dropdown-terminal"
INTERVAL = 0.2  # seconds between focus polls
STARTUP_GRACE = 1.0  # let Hyprland finish opening before watching

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CLOSE_CMD = os.path.join(PLUGIN_ROOT, "bin", "omarchy-dropdown-terminal")


def _runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


LOCK_PATH = os.path.join(_runtime_dir(), "dropdown-terminal-watch.lock")


def _ensure_hypr_env():
    """hyprctl needs HYPRLAND_INSTANCE_SIGNATURE; discover it when absent."""
    rt = _runtime_dir()
    os.environ.setdefault("XDG_RUNTIME_DIR", rt)
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return
    try:
        for entry in sorted(os.listdir(os.path.join(rt, "hypr"))):
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = entry
            return
    except OSError:
        pass  # hyprctl will simply report "not focused" rather than crash


def _run(argv, timeout=5):
    """Run an argv-array subprocess, never a shell. Returns None on failure."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def is_dropdown_focused():
    res = _run(["hyprctl", "activewindow", "-j"], timeout=3)
    if res is None:
        return False
    try:
        return json.loads(res.stdout).get("class") == APP_ID
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def hide_dropdown():
    """Hide via the plugin CLI. Guarded there, so a no-op when already hidden."""
    for _ in range(2):
        res = _run([CLOSE_CMD, "close"])
        if res is not None and res.returncode == 0:
            return True
        time.sleep(0.2)
    print("focus_watcher: 'omarchy-dropdown-terminal close' failed", file=sys.stderr)
    return False


def main():
    # Single instance only: the close path toggles, so a second watcher firing
    # on the same focus change could re-open what the first one just closed.
    try:
        lock = open(LOCK_PATH, "w")
    except OSError:
        # Unwritable runtime dir: run anyway rather than silently doing
        # nothing. Better a rare duplicate than a dead watcher.
        lock = None
    if lock is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another watcher is already running

    _ensure_hypr_env()

    time.sleep(STARTUP_GRACE)
    was_focused = is_dropdown_focused()
    while True:
        time.sleep(INTERVAL)
        focused = is_dropdown_focused()
        if was_focused and not focused:
            hide_dropdown()
        was_focused = focused


if __name__ == "__main__":
    main()
