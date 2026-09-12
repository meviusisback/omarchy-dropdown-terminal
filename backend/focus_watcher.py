#!/usr/bin/env python3
"""
Watch the dropdown terminal's focus. When it loses focus, close the
special workspace so the user can interact with the desktop underneath.

Runs as a lightweight poller (~4 Hz). Started by the plugin on
Component.onCompleted, stopped when the plugin unloads.
"""
import json
import subprocess
import sys
import time

APP_ID = "org.omarchy.dropdown-terminal"
SPECIAL_WS = "special:dropdown"
INTERVAL = 0.25  # seconds
STARTUP_GRACE = 1.0  # seconds to wait before acting on focus changes


def _run(argv):
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=3, check=False
        )
        return r.stdout
    except Exception:
        return ""


def is_dropdown_focused():
    out = _run(["hyprctl", "activewindow", "-j"])
    try:
        data = json.loads(out)
        return data.get("class") == APP_ID
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def is_special_ws_active():
    out = _run(["hyprctl", "monitors", "-j"])
    try:
        data = json.loads(out)
        for m in data:
            sws = (m.get("specialWorkspace") or {})
            if sws.get("name") == SPECIAL_WS and sws.get("id", 0) != 0:
                return True
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return False


def close_special_ws():
    _run(["hyprctl", "dispatch", "togglespecialworkspace", "dropdown"])


def main():
    # Grace period: on startup, wait for the dropdown to appear and
    # gain focus before we start watching. This avoids a race where
    # the watcher sees "not focused" before Hyprland finishes opening.
    time.sleep(STARTUP_GRACE)

    was_focused = is_dropdown_focused()
    while True:
        time.sleep(INTERVAL)
        focused = is_dropdown_focused()
        if was_focused and not focused:
            # Focus just left the dropdown — close the special workspace
            # so the desktop becomes interactive.
            if is_special_ws_active():
                close_special_ws()
        was_focused = focused


if __name__ == "__main__":
    main()
