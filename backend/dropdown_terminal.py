#!/usr/bin/env python3
"""
Drop-down terminal backend for Omarchy (Hyprland Lua engine).

Installs/uninstalls:
  - ~/.config/hypr/dropdown-terminal.lua       (window rules for the dropdown)
  - hook line in ~/.config/hypr/hyprland.lua   (dofile wrapper, workspace-layout pattern)
  - o.bind line in ~/.config/hypr/bindings.lua (SUPER + U)
  - ~/.config/systemd/user/foot-server@.service (parameterized foot server unit)

Design guarantees:
  - Every rewrite is atomic: temp file + os.replace, preserving mode/ownership.
  - All config-file edits are serialized with an flock'd lockfile.
  - Idempotency keys off plugin-owned marker comments, never loose substrings.
  - No user-controlled data is interpolated into any written file; the rules,
    hook, bind, and unit files are fixed constants.
  - Subprocess calls are argv arrays only; no shell=True anywhere.
"""

import fcntl
import json
import os
import stat as stat_module
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
MARKER = "meviusisback.dropdown-terminal"

HYPRLAND_DIR = os.path.join(HOME, ".config", "hypr")
RULES_PATH = os.path.join(HYPRLAND_DIR, "dropdown-terminal.lua")
HYPRLAND_LUA = os.path.join(HYPRLAND_DIR, "hyprland.lua")
BINDINGS_LUA = os.path.join(HYPRLAND_DIR, "bindings.lua")
LOCK_PATH = os.path.join(HYPRLAND_DIR, ".dropdown-terminal.lock")

SYSTEMD_USER_DIR = os.path.join(HOME, ".config", "systemd", "user")
UNIT_SRC_NAME = "foot-server@.service"
UNIT_DST_PATH = os.path.join(SYSTEMD_USER_DIR, UNIT_SRC_NAME)
UNIT_INSTANCE = "dropdown-terminal"
UNIT_REF = f"foot-server@{UNIT_INSTANCE}.service"

DROPDOWN_APP_ID = "org.omarchy.dropdown-terminal"
DROPDOWN_WS = "dropdown"

RULES_BEGIN = f"-- BEGIN {MARKER} (generated; do not edit)"
RULES_END = f"-- END {MARKER}"
UNIT_BEGIN = f"# BEGIN {MARKER} (foot server unit)"
UNIT_END = f"# END {MARKER}"

UNIT_BODY = f"""\
{UNIT_BEGIN}
[Unit]
Description=Foot terminal server (drop-down terminal, instance %i)
PartOf=graphical-session.target
After=graphical-session.target
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
ExecStart=/usr/bin/foot --server=%t/foot-%i.sock --app-id={DROPDOWN_APP_ID}
Restart=on-failure
NonBlocking=true
UnsetEnvironment=LISTEN_PID LISTEN_FDS LISTEN_FDNAMES

[Install]
WantedBy=graphical-session.target
{UNIT_END}
"""

RULES_BODY = f"""\
{RULES_BEGIN}
-- Drop-down terminal: centered floating panel on its own special workspace.
-- Toggling the workspace plays Hyprland's specialWorkspace slidevert
-- animation. Float + the workspace assignment keep it out of the tiling
-- flow. The special workspace blocks desktop interaction, so a focus
-- watcher (focus_watcher.py) auto-closes it when focus leaves the terminal.
-- NO pin/stay_focused: pin would turn a stray spawn into an always-on-top
-- overlay, and stay_focused would glue keyboard focus to it.
local ddws = "special:{DROPDOWN_WS}"

o.window("{DROPDOWN_APP_ID}", {{
  workspace = ddws,
  float = true,
  size = {{ "(monitor_w*80/100)", "(monitor_h*45/100)" }},
  move = {{ "(monitor_w*10/100)", "36" }},
  animation = "slide top",
  border_size = 3,
  rounding = 8,
}})
{RULES_END}
"""

HOOK_LINE = (
    f"-- Added by the {MARKER} plugin: installs the drop-down terminal window rules.\n"
    'do local path = (os.getenv("XDG_CONFIG_HOME") or os.getenv("HOME") .. "/.config") '
    '.. "/hypr/dropdown-terminal.lua"; local file = io.open(path, "r"); '
    "if file then file:close(); dofile(path) end end\n"
)

BIND_BEGIN = f"-- BEGIN {MARKER}"
BIND_END = f"-- END {MARKER}"
BIND_BODY = 'o.bind("SUPER + U", "Toggle drop-down terminal", "omarchy-dropdown-terminal toggle")'
BIND_LINE = f"{BIND_BEGIN}\n{BIND_BODY}\n{BIND_END}\n"
BIND_TAG = "Toggle drop-down terminal"

# ----------------------------------------------------------------- utilities


def _run(argv, timeout=10):
    """Run an argv-array subprocess, never a shell. Never raises."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        class _Failed:
            returncode = 1
            stdout = ""
            stderr = f"could not run {argv[0]}"
        return _Failed()


def _locked(path, mode="a+"):
    """Open an exclusive lockfile context manager (flock)."""
    class _Lock:
        def __enter__(self):
            self.fd = open(path, mode)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self.fd

        def __exit__(self, *exc):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.fd.close()
            return False

    return _Lock()


def atomic_write(path, content, mode=0o644):
    """Write content to path atomically; preserve existing mode/ownership."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ddterm-")
    try:
        with os.fdopen(tmp_fd, "w") as tmp:
            tmp.write(content)
        try:
            st = os.stat(path)
            os.chmod(tmp_path, stat_module.S_IMODE(st.st_mode))
            os.chown(tmp_path, st.st_uid, st.st_gid)
        except FileNotFoundError:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def read_text(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return None


def append_block(path, begin, body, end):
    """Append a BEGIN/body/END block if its marker is absent. Serialized."""
    with _locked(LOCK_PATH) as _:
        content = read_text(path) or ""
        if begin in content:
            return False
        if content and not content.endswith("\n"):
            content += "\n"
        content += begin + "\n" + body + end + "\n"
        atomic_write(path, content)
        return True


def remove_block(path, begin, end):
    """Remove a BEGIN..END block. Serialized, atomic, refuses to over-strip."""
    with _locked(LOCK_PATH) as _:
        content = read_text(path)
        if content is None:
            return False
        lines = content.splitlines(keepends=True)
        out, inside, changed = [], False, False
        for ln in lines:
            if not inside and begin in ln:
                inside = True
                changed = True
                continue
            if inside and end in ln:
                inside = False
                changed = True
                continue
            if not inside:
                out.append(ln)
        if inside:
            raise ValueError(f"unterminated marker block in {path}")
        if changed:
            atomic_write(path, "".join(out))
        return changed


def line_present(path, needle):
    content = read_text(path) or ""
    return any(needle in ln for ln in content.splitlines())


# -------------------------------------------------------------------- install


def check_keybind_conflict():
    """Warn if SUPER + U is claimed by any other o.bind line outside our block."""
    content = read_text(BINDINGS_LUA) or ""
    for ln in content.splitlines():
        s = ln.strip()
        if s.startswith("--"):
            continue
        if "SUPER + U" in ln and BIND_TAG not in ln and "dropdown-terminal" not in ln:
            return ln.strip()[:120]
    return None


def install_unit():
    """Install the foot-server@ template unit, backing up a foreign file."""
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    existing = read_text(UNIT_DST_PATH)
    if existing is not None and MARKER not in existing:
        backup = UNIT_DST_PATH + ".pre-dropdown-terminal.bak"
        if not os.path.exists(backup):
            with open(UNIT_DST_PATH, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
            print(f"backed up existing {UNIT_DST_PATH} -> {backup}")
    atomic_write(UNIT_DST_PATH, UNIT_BODY)
    _run(["systemctl", "--user", "daemon-reload"])
    return True


def enable_server():
    """Enable + start the dropdown foot server instance."""
    _run(["systemctl", "--user", "enable", "--now", UNIT_REF])


def install(quiet=False):
    results = {}

    # 1. Hyprland rules file (always regenerated, marker-checked).
    atomic_write(RULES_PATH, RULES_BODY)
    results["rules_file"] = RULES_PATH

    # 2. Hook line in hyprland.lua (keys off the full hook line).
    hook_anchor = f'"/hypr/dropdown-terminal.lua"'
    if not line_present(HYPRLAND_LUA, f"-- Added by the {MARKER} plugin"):
        with _locked(LOCK_PATH) as _:
            content = read_text(HYPRLAND_LUA) or ""
            if hook_anchor not in content:
                if content and not content.endswith("\n"):
                    content += "\n"
                content += HOOK_LINE
                atomic_write(HYPRLAND_LUA, content)
                results["hook"] = "added"
            else:
                results["hook"] = "hook-line-present-without-marker"
    else:
        results["hook"] = "already-installed"

    # 3. Keybind block in bindings.lua.
    conflict = check_keybind_conflict()
    if conflict:
        results["keybind"] = f"CONFLICT: SUPER + U already used by: {conflict}"
    else:
        added = append_block(BINDINGS_LUA, BIND_BEGIN, BIND_BODY, BIND_END)
        results["keybind"] = "added" if added else "already-installed"

    # 4. systemd unit + enable (enable skipped under DDT_TEST: hermetic tests).
    install_unit()
    if os.environ.get("DDT_TEST") != "1":
        enable_server()
    results["unit"] = UNIT_REF

    if not quiet:
        print(json.dumps(results, indent=2))
    return results


def uninstall():
    results = {}

    # 1. Stop + disable the server (ordered to defeat Restart respawn).
    _run(["systemctl", "--user", "disable", "--now", UNIT_REF])
    for _ in range(20):
        if _run(["systemctl", "--user", "is-active", UNIT_REF]).stdout.strip() != "active":
            break
        time.sleep(0.25)
    _run(["systemctl", "--user", "kill", "--kill-whom=main", UNIT_REF])
    _run(["systemctl", "--user", "mask", UNIT_REF])
    results["unit"] = "stopped+disabled+masked"

    # 2. Remove the keybind block, the hook line, the rules file.
    results["keybind_removed"] = remove_block(BINDINGS_LUA, BIND_BEGIN, BIND_END)
    results["hook_removed"] = _remove_hook_line()
    try:
        os.unlink(RULES_PATH)
        results["rules_file"] = "removed"
    except FileNotFoundError:
        results["rules_file"] = "absent"

    # 3. Unmask + remove unit file + reload.
    _run(["systemctl", "--user", "unmask", UNIT_REF])
    try:
        os.unlink(UNIT_DST_PATH)
        results["unit_file"] = "removed"
    except FileNotFoundError:
        results["unit_file"] = "absent"
    _run(["systemctl", "--user", "daemon-reload"])

    try:
        os.unlink(LOCK_PATH)
    except FileNotFoundError:
        pass

    print(json.dumps(results, indent=2))
    return results


def _remove_hook_line():
    """Remove the two hook lines (comment + dofile) from hyprland.lua."""
    with _locked(LOCK_PATH) as _:
        content = read_text(HYPRLAND_LUA)
        if content is None or MARKER not in content:
            return False
        kept = [
            ln for ln in content.splitlines(keepends=True)
            if MARKER not in ln and '/hypr/dropdown-terminal.lua' not in ln
        ]
        atomic_write(HYPRLAND_LUA, "".join(kept))
        return True


# --------------------------------------------------------------------- status


def status():
    out = {"server": "unknown", "window": None, "workspace": None, "keybind": "SUPER + U"}
    unit = _run(["systemctl", "--user", "is-active", UNIT_REF]).stdout.strip()
    out["server"] = unit or "unknown"

    clients = _run(["hyprctl", "clients", "-j"]).stdout
    try:
        data = json.loads(clients) if clients else []
        for c in data:
            if c.get("class") == DROPDOWN_APP_ID:
                out["window"] = c.get("address")
                out["workspace"] = (c.get("workspace") or {}).get("name")
                break
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass  # hyprctl unavailable or malformed output: keep safe fallback

    mons = _run(["hyprctl", "monitors", "-j"]).stdout
    try:
        mdata = json.loads(mons) if mons else []
        for m in mdata:
            ws = (m.get("specialWorkspace") or {}).get("name") or ""
            if ws.endswith(DROPDOWN_WS):
                out["visible"] = True
                break
        else:
            out["visible"] = False
    except (json.JSONDecodeError, AttributeError, TypeError):
        out["visible"] = False

    print(json.dumps(out, indent=2))
    return out


# ----------------------------------------------------------------------- main


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        status()
    elif cmd == "check-conflict":
        conflict = check_keybind_conflict()
        print(conflict or "no conflict")
        sys.exit(1 if conflict else 0)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
