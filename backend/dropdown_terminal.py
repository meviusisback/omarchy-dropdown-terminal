#!/usr/bin/python3
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
  - Only validated values reach written files: the rules/hook/bind bodies are
    fixed constants, and the unit's ExecStart interpolates the foot path that
    proc.resolve() validated (never user input).
  - Subprocess calls are argv arrays only; no shell=True anywhere.
  - Every external tool is resolved to a validated absolute path (never PATH)
    and runs with a minimal environment and bounded output - see proc.py.
"""

import contextlib
import fcntl
import json
import os
import pwd
import re
import stat as stat_module
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import proc  # noqa: E402  (sibling module: trusted plugin code)


def _validated_home():
    """The user's home directory, or None when it cannot be established safely.

    Every path this backend writes derives from HOME, so an empty or relative value
    would scatter config into the filesystem root or the cwd. Accept only an
    absolute, existing directory owned by this user; otherwise fall back to the
    passwd entry. Callers that write must use require_home()/_require_under_home():
    resolving HOME must NOT gate commands that do not touch it (runtime-dir,
    socket-path, state-path - the widget asks for those, and the watcher tolerates
    an unusable HOME, so failing here would make the two disagree).
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
        if stat_module.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            return candidate.rstrip("/") or "/"
    return None


HOME = _validated_home() or ""


def require_home():
    """The validated home directory, or a loud failure for commands that need it."""
    if not HOME:
        raise SystemExit(
            "cannot determine a safe HOME (unset, relative, missing, or not owned by "
            "this user): refusing to write any config"
        )
    return HOME


def _config_dir_candidate(ancestor_uids=None):
    """The validated XDG_CONFIG_HOME, or None when it is unset/unusable.

    A directory that does not exist yet is acceptable (a first install creates it) as
    long as its parent chain is safe; world-writable or foreign-owned candidates are
    not, because their name could be swapped.
    """
    candidate = os.environ.get("XDG_CONFIG_HOME", "")
    if not candidate or not os.path.isabs(candidate):
        return None
    import focus_watcher  # same ancestor rule the runtime dir uses

    real = os.path.realpath(candidate)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        parent = os.path.dirname(real)
        if not parent or not os.path.isdir(parent):
            return None
        st = os.stat(parent)
        real = parent
    except OSError:
        return None
    if not stat_module.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return None
    if st.st_mode & 0o022:
        return None      # group/world-writable: its name can be swapped
    if not focus_watcher._ancestors_safe(real, ancestor_uids or (0, os.getuid())):
        return None
    return os.path.realpath(candidate)


def _validated_config_dir(ancestor_uids=None):
    """Where this user's config lives: validated XDG_CONFIG_HOME, else $HOME/.config.

    Hyprland (and the Lua hook) find the config through XDG_CONFIG_HOME, so writing
    to $HOME/.config while that variable points elsewhere would install rules the
    compositor never loads. Callers that WRITE use require_config_dir(), which fails
    loudly instead of silently falling back, so a disagreement cannot be created.
    """
    candidate = _config_dir_candidate(ancestor_uids)
    if candidate:
        return candidate
    return os.path.join(HOME, ".config") if HOME else ""


def require_config_dir(ancestor_uids=None):
    """The config directory for write commands, or a loud failure."""
    if os.environ.get("XDG_CONFIG_HOME"):
        candidate = _config_dir_candidate(ancestor_uids)
        if candidate is None:
            raise SystemExit(
                "XDG_CONFIG_HOME is set but is not an absolute, self-owned directory "
                "with safe ancestors: refusing to install anywhere the compositor "
                "would not read"
            )
        return candidate
    return require_home() and os.path.join(HOME, ".config")


def _require_safe_write_target(path):
    """Guard every write: inside HOME or the validated config directory.

    Compared on RESOLVED paths, so a symlinked directory component cannot smuggle a
    write outside those trees, and the write commands additionally require the config
    directory itself to be the validated one (require_config_dir).
    """
    require_config_dir()
    allowed = [base for base in (HOME, _validated_config_dir()) if base]
    if not allowed:
        raise SystemExit(
            "cannot determine a safe HOME (unset, relative, missing, or not owned by "
            "this user): refusing to write any config"
        )
    real = os.path.realpath(path)
    if not os.path.isabs(path) or not any(
        real.startswith(os.path.realpath(base) + os.sep) for base in allowed
    ):
        raise SystemExit(f"refusing to write {path!r}: outside {' and '.join(allowed)}")

# Ceiling for reading our own config files; see read_text().
MAX_CONFIG_BYTES = 1 << 20
MARKER = "meviusisback.dropdown-terminal"

# Window addresses are echoed to the bar widget; only the compositor's own shape.
WINDOW_RE = re.compile(r"\A0x[0-9a-fA-F]{1,32}\Z")

HYPRLAND_DIR = os.path.join(_validated_config_dir(), "hypr")
RULES_PATH = os.path.join(HYPRLAND_DIR, "dropdown-terminal.lua")
HYPRLAND_LUA = os.path.join(HYPRLAND_DIR, "hyprland.lua")
BINDINGS_LUA = os.path.join(HYPRLAND_DIR, "bindings.lua")
LOCK_PATH = os.path.join(HYPRLAND_DIR, ".dropdown-terminal.lock")

SYSTEMD_USER_DIR = os.path.join(_validated_config_dir(), "systemd", "user")
UNIT_SRC_NAME = "foot-server@.service"
UNIT_DST_PATH = os.path.join(SYSTEMD_USER_DIR, UNIT_SRC_NAME)
UNIT_INSTANCE = "dropdown-terminal"
UNIT_REF = f"foot-server@{UNIT_INSTANCE}.service"
# The unit binds `--server=%t/foot-%i.sock`; with our instance that is this name.
# Kept as a constant so the CLI's socket path and the unit cannot drift apart
# (a test substitutes UNIT_INSTANCE into the unit template and compares).
FOOT_SOCKET_NAME = f"foot-{UNIT_INSTANCE}.sock"

DROPDOWN_APP_ID = "org.omarchy.dropdown-terminal"
DROPDOWN_WS = "dropdown"

RULES_BEGIN = f"-- BEGIN {MARKER} (generated; do not edit)"
RULES_END = f"-- END {MARKER}"
UNIT_BEGIN = f"# BEGIN {MARKER} (foot server unit)"
UNIT_END = f"# END {MARKER}"

def unit_body(foot):
    """The systemd unit, with the RESOLVED foot path substituted in.

    The binary the unit actually executes must be the one that was validated, not
    a hardcoded path that might resolve elsewhere (proc.resolve could legitimately
    have chosen /usr/local/bin/foot).
    """
    return f"""\
{UNIT_BEGIN}
[Unit]
Description=Foot terminal server (drop-down terminal, instance %i)
PartOf=graphical-session.target
After=graphical-session.target
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
ExecStart={foot} --server=%t/foot-%i.sock --app-id={DROPDOWN_APP_ID}
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
-- Toggling the workspace plays Hyprland's specialWorkspace animation, whose
-- direction the hl.animation() calls below set to top/bottom so the panel
-- DROPS IN from the top edge and retracts upward. Float + the workspace
-- assignment keep it out of the tiling flow. NO pin/stay_focused: pin would
-- turn a stray spawn into an always-on-top overlay, and stay_focused would
-- glue keyboard focus to it.
local ddws = "special:{DROPDOWN_WS}"

-- Two global input settings the dropdown needs (both reverted on uninstall):
--
-- special_fallthrough: a floating window on a special workspace otherwise
--   blocks focusing regular windows ("having only floating windows in the
--   special workspace will not block focusing windows in the regular
--   workspace"), so a click outside the terminal would do nothing.
-- follow_mouse = 0 / float_switch_override_focus = 0: make dismissal
--   CLICK-based. With cursor-follows-focus on (the Omarchy default), merely
--   moving the mouse over another window focuses it, so the dropdown would
--   close on hover; with focus moved only by a click, the focus watcher
--   (focus_watcher.py) closes it exactly when you click away.
if hl and hl.config then
  hl.config({{
    input = {{
      special_fallthrough = true,
      follow_mouse = 0,
      float_switch_override_focus = 0,
    }},
  }})
end

-- Animation direction of the drop (global per leaf, reverted on uninstall).
--
-- The style string carries a direction token ("top"/"bottom"/"left"/"right")
-- that overrides the direction the compositor passes in, and Hyprland starts a
-- special workspace a screen BELOW on IN (Monitor.cpp: left = true), so
-- Omarchy's bare "slidevert" default makes every special workspace rise from
-- the bottom. IN with "top" begins one screen above and animates down to rest;
-- OUT with "bottom" animates back up, i.e. it retracts the way it came.
--
-- Caution: hl.animation is per LEAF, so this restyles every special workspace
-- on the monitor - the Omarchy scratchpad (SUPER + S) included. The engine has
-- no per-workspace animation override. Setting these two leaves also marks them
-- overridden, so they no longer inherit later changes to Omarchy's
-- `specialWorkspace` line.
--
-- The bezier is Omarchy's own curve (defined in its looknfeel.lua, which loads
-- before this file), so the motion matches the rest of the shell. A curve that
-- fails to resolve is reported by `hyprctl configerrors`, and the direction pin
-- would then simply not apply.
if hl and hl.animation then
  hl.animation({{ leaf = "specialWorkspaceIn", enabled = true, speed = 3, bezier = "easeOutQuint", style = "slidevert top" }})
  hl.animation({{ leaf = "specialWorkspaceOut", enabled = true, speed = 3, bezier = "easeOutQuint", style = "slidevert bottom" }})
end

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
# The keybind must not go through PATH: Omarchy's o.bind turns a string dispatcher
# into hl.dsp.exec_cmd, i.e. a shell command, which would be resolved by the
# compositor's PATH - a writable earlier entry would shadow it. The installer also
# symlinks the CLI into ~/.local/bin, so that absolute path is stable.
SAFE_KEYBIND_PATH_RE = re.compile(r"\A/[A-Za-z0-9._+/-]+\Z")


def _lua_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def cli_command():
    """The absolute CLI path for the keybind, or a loud failure.

    The path is interpolated into a Lua string that becomes a shell command, so HOME
    must not contain anything that could break out of either (a space, quote,
    backslash, `;`, `$`...). Refusing is better than writing a keybind that is broken
    or, worse, executes something else.
    """
    if not HOME:
        raise SystemExit("cannot determine a safe HOME: refusing to write the keybind")
    path = os.path.join(HOME, ".local", "bin", "omarchy-dropdown-terminal")
    if not SAFE_KEYBIND_PATH_RE.match(path):
        raise SystemExit(
            f"HOME cannot be written into a keybind safely ({HOME!r}): it contains "
            "characters that would need shell/Lua escaping"
        )
    return _lua_escape(path)


def bind_body():
    return f'o.bind("SUPER + U", "Toggle drop-down terminal", "{cli_command()} toggle")'


def bind_line():
    """The BEGIN/body/END block. A function, not a constant: it must not resolve HOME
    at import time (a session with an unusable HOME must still be able to run
    `runtime-dir`/`socket-path`/`close`)."""
    return f"{BIND_BEGIN}\n{bind_body()}\n{BIND_END}\n"


BIND_TAG = "Toggle drop-down terminal"

# ----------------------------------------------------------------- utilities


# Every _call() argv, recorded so the test suite can assert that no call site
# passes a bare tool name; the suite substitutes proc.run so nothing is executed.
CALLS = []


class _Failed:
    """Stand-in for a subprocess result when a call could not be made at all."""

    def __init__(self, stderr):
        self.returncode = 1
        self.stdout = ""
        self.stderr = stderr


def _tool(name):
    """Absolute path of a trusted system tool, or raise RuntimeError."""
    path = proc.resolve(name)
    if path is None:
        raise RuntimeError(
            f"no trusted {name!r} found in {' or '.join(proc.CANDIDATE_DIRS)} "
            "(root-owned, not group/world-writable)"
        )
    return path


def _call(name, *args, timeout=10):
    """Run a trusted system tool with a minimal environment and bounded output.

    `name` is resolved to a validated absolute path (never through PATH); a tool
    that cannot be resolved fails safely rather than being looked up by the OS.
    The argv is appended to CALLS for the test suite, which substitutes proc.run
    so that a test run never executes systemctl against the developer's session.
    """
    try:
        binary = _tool(name)
    except RuntimeError as exc:
        return _Failed(str(exc))
    argv = [binary, *args]
    CALLS.append(argv)
    try:
        return proc.run(argv, timeout=timeout)
    except (ValueError, OSError, proc.ToolNotFound) as exc:
        return _Failed(f"could not run {name}: {exc}")


def _run(argv, timeout=10):
    """Legacy argv-array entry point. Unused by the plugin itself: proc.run() is
    the single execution path (kept only so an out-of-tree caller cannot crash on
    a missing symbol)."""
    try:
        return proc.run(argv, timeout=timeout)
    except (ValueError, OSError, proc.ToolNotFound) as exc:
        return _Failed(str(exc))


@contextlib.contextmanager
def _locked(path):
    """Serialise config edits behind an exclusive flock'd lockfile.

    Created 0600 with O_NOFOLLOW (a symlink planted at the lock path would
    otherwise let the plugin open and lock an arbitrary file the user owns) and
    O_CLOEXEC so no spawned child inherits the descriptor. Guarded like every other
    write, so an unusable HOME cannot create a relative lock file in the cwd.
    """
    _require_safe_write_target(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot open the lock file {path}: {exc}") from exc
    handle = os.fdopen(fd, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield handle
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write(path, content, mode=0o644):
    """Write content to path atomically; preserve existing mode/ownership."""
    _require_safe_write_target(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ddterm-")
    try:
        with os.fdopen(tmp_fd, "w") as tmp:
            tmp.write(content)
        try:
            # lstat, not stat: a symlink at the write target must be REPLACED by
            # os.replace, not followed for mode/ownership (following it would chown
            # to the link target's owner and raise EPERM on a foreign-owned link).
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        try:
            if st is not None and not stat_module.S_ISLNK(st.st_mode):
                os.chmod(tmp_path, stat_module.S_IMODE(st.st_mode))
                os.chown(tmp_path, st.st_uid, st.st_gid)
            else:
                os.chmod(tmp_path, mode)
        except OSError:
            # Best effort: the content matters more than preserving a mode we cannot
            # copy (e.g. a target owned by root).
            pass
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


class ConfigUnreadable(RuntimeError):
    """A config file exists but cannot be read (or is absurdly large).

    Distinct from "absent" on purpose: callers that regenerate a missing file must
    NOT treat "I could not read it" the same way, or a 2 MiB hyprland.lua would be
    replaced by our two hook lines. Every such caller fails loudly instead.
    """


def read_text(path, limit=MAX_CONFIG_BYTES):
    """Read a config file. None = absent (safe to create); raises if unreadable.

    The path must be a regular file (or a symlink TO one): a FIFO or device at a
    config path would otherwise block an automatic install forever, and a byte cap
    only bounds regular files.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigUnreadable(f"cannot inspect {path}: {exc}") from exc
    if not stat_module.S_ISREG(st.st_mode):
        if stat_module.S_ISLNK(st.st_mode):
            try:
                st = os.stat(path)
            except OSError as exc:
                raise ConfigUnreadable(f"cannot follow {path}: {exc}") from exc
        if not stat_module.S_ISREG(st.st_mode):
            raise ConfigUnreadable(f"{path} is not a regular file; refusing to read it")
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise ConfigUnreadable(f"cannot read {path}: {exc}") from exc
    if len(data) > limit:
        raise ConfigUnreadable(f"{path} is larger than {limit} bytes; refusing to touch it")
    return data.decode("utf-8", "replace")


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
    require_home()
    content = read_text(BINDINGS_LUA) or ""
    for ln in content.splitlines():
        s = ln.strip()
        if s.startswith("--"):
            continue
        if "SUPER + U" in ln and BIND_TAG not in ln and "dropdown-terminal" not in ln:
            return ln.strip()[:120]
    return None


def install_unit(foot=None):
    """Install the foot-server@ template unit, backing up a foreign file."""
    if foot is None:
        foot = proc.resolve("foot")
    if foot is None:
        raise SystemExit("cannot install the foot server unit: no trusted foot binary")
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    existing = read_text(UNIT_DST_PATH)
    if existing is not None and MARKER not in existing:
        backup = UNIT_DST_PATH + ".pre-dropdown-terminal.bak"
        # atomic_write (mkstemp + replace) rather than open(..., "wb"): a plantable
        # symlink at the backup path must be replaced, not followed to a file of
        # someone else's choosing. read_text already bounded and validated the source.
        if not os.path.exists(backup) or os.path.islink(backup):
            atomic_write(backup, existing)
            print(f"backed up existing {UNIT_DST_PATH} -> {backup}")
    atomic_write(UNIT_DST_PATH, unit_body(foot))
    _call("systemctl", "--user", "daemon-reload")
    return True


def enable_server():
    """Enable + start the dropdown foot server instance."""
    _call("systemctl", "--user", "enable", "--now", UNIT_REF)


def install(quiet=False):
    results = {}
    require_home()

    # 0a. Read (and therefore validate) every file we are about to modify BEFORE
    #     writing anything: an unreadable or oversized config aborts the install with
    #     nothing half-applied - including this plugin's own rules file, which used to
    #     be written before the check.
    for path in (UNIT_DST_PATH, HYPRLAND_LUA, BINDINGS_LUA):
        read_text(path)

    # 0. The plugin's own executables are pinned by absolute shebang and the
    #    generated unit will use the resolved foot path, so verify those
    #    interpreters/binaries exist at trusted absolute paths before writing
    #    anything: an unsupported layout must fail loudly, not leave a
    #    half-installed plugin.
    tools = {name: proc.resolve(name) for name in ("python3", "bash", "foot")}
    missing = sorted(name for name, path in tools.items() if path is None)
    if missing:
        raise SystemExit(
            "missing trusted tools: "
            + ", ".join(missing)
            + " (looked in /usr/bin, /bin, /usr/local/bin; must be root-owned and"
            " not group/world-writable)"
        )
    results["tools"] = tools

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
        added = append_block(BINDINGS_LUA, BIND_BEGIN, bind_body(), BIND_END)
        results["keybind"] = "added" if added else "already-installed"

    # 4. systemd unit (with the validated foot path) + enable.
    install_unit(tools["foot"])
    enable_server()
    results["unit"] = UNIT_REF

    if not quiet:
        print(json.dumps(results, indent=2))
    return results


def uninstall():
    results = {}
    require_home()

    # Validate the configs BEFORE touching systemd: an unreadable file must not leave
    # the unit disabled+masked with our keybind block still installed and no way to
    # retry (the previous order disabled the unit first and then raised).
    for path in (UNIT_DST_PATH, HYPRLAND_LUA, BINDINGS_LUA, RULES_PATH):
        read_text(path)

    # 1. Stop + disable the server (ordered to defeat Restart respawn).
    _call("systemctl", "--user", "disable", "--now", UNIT_REF)
    for _ in range(20):
        if _call("systemctl", "--user", "is-active", UNIT_REF).stdout.strip() != "active":
            break
        time.sleep(0.25)
    _call("systemctl", "--user", "kill", "--kill-whom=main", UNIT_REF)
    _call("systemctl", "--user", "mask", UNIT_REF)
    results["unit"] = "stopped+disabled+masked"

    # 2. Remove the keybind block, the hook line, the rules file. A corrupted marker
    #    block is reported, not fatal: aborting here would leave the unit masked (step
    #    3 never runs) and every retry would fail at the same place.
    try:
        results["keybind_removed"] = remove_block(BINDINGS_LUA, BIND_BEGIN, BIND_END)
    except ValueError as exc:
        results["keybind_removed"] = f"skipped: {exc}"
    try:
        results["hook_removed"] = _remove_hook_line()
    except ValueError as exc:
        results["hook_removed"] = f"skipped: {exc}"
    try:
        os.unlink(RULES_PATH)
        results["rules_file"] = "removed"
    except FileNotFoundError:
        results["rules_file"] = "absent"

    # 3. Unmask + remove unit file + reload. In a finally: whatever happened above,
    #    a masked unit must never survive an uninstall (it would block a reinstall).
    try:
        try:
            os.unlink(UNIT_DST_PATH)
            results["unit_file"] = "removed"
        except FileNotFoundError:
            results["unit_file"] = "absent"
    finally:
        _call("systemctl", "--user", "unmask", UNIT_REF)
        _call("systemctl", "--user", "daemon-reload")

    # The lock file is deliberately NOT removed: unlinking a lock another process
    # may still hold would let a fresh process create a different inode and both
    # proceed, which is the split-brain the lock exists to prevent.

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


def _bounded(value, limit=64):
    """Cap a compositor-supplied string before it is echoed to the widget.

    The bar widget reads this JSON as text with no size cap (Quickshell 0.3.1's
    StdioCollector has no maxBufferSize), so a hostile or broken compositor must not
    be able to hand it a megabyte-long "address" or workspace name.
    """
    if value is None:
        return None
    return str(value)[:limit]


def window_address(value):
    """The address, or None - validated, not truncated: only the compositor's shape.

    Truncating first would turn a 4 KB hex string into something that still looks
    like an address, so the length is part of the validation.
    """
    text = "" if value is None else str(value)
    return text if WINDOW_RE.match(text) else None


def status():
    out = {"server": "unknown", "window": None, "workspace": None, "keybind": "SUPER + U"}
    unit = _bounded(_call("systemctl", "--user", "is-active", UNIT_REF).stdout.strip(), 32)
    out["server"] = unit or "unknown"

    clients = _call("hyprctl", "clients", "-j").stdout
    try:
        data = json.loads(clients) if clients else []
        for c in data:
            if c.get("class") == DROPDOWN_APP_ID:
                out["window"] = window_address(c.get("address"))
                out["workspace"] = _bounded((c.get("workspace") or {}).get("name"))
                break
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass  # hyprctl unavailable or malformed output: keep safe fallback

    mons = _call("hyprctl", "monitors", "-j").stdout
    try:
        mdata = json.loads(mons) if mons else []
        for m in mdata:
            ws = (m.get("specialWorkspace") or {}).get("name") or ""
            if ws == f"special:{DROPDOWN_WS}":
                out["visible"] = True
                break
        else:
            out["visible"] = False
    except (json.JSONDecodeError, AttributeError, TypeError):
        out["visible"] = False

    print(json.dumps(out, indent=2))
    return out


def _validated_runtime(ancestor_uids=None, prefer_systemd=False):
    """The runtime directory from the single validator, or a loud failure."""
    import focus_watcher  # sibling module: owns the runtime-dir validation

    runtime = focus_watcher.runtime_dir(ancestor_uids, prefer_systemd)
    if runtime is None:
        raise SystemExit(
            "no safe runtime directory (need an absolute, self-owned dir with no "
            "group/other bits, ancestors not writable by others, and not HOME)"
        )
    return runtime


def runtime_paths(ancestor_uids=None):
    """The validated runtime directory and the state file inside it.

    Single source of truth: the CLI (for the foot client socket) and Panel.qml (for
    the state file the widget watches) both ask here instead of re-implementing the
    rules, so the three consumers cannot disagree about which directory is safe.
    `ancestor_uids` is only for tests (see focus_watcher._ancestors_safe).
    """
    import focus_watcher  # sibling module: owns the runtime-dir validation

    runtime = _validated_runtime(ancestor_uids)
    return runtime, os.path.join(runtime, focus_watcher.STATE_NAME)


def socket_path(ancestor_uids=None):
    """The foot client socket path, in the directory the UNIT actually binds.

    The name must stay in step with `--server=%t/foot-%i.sock` for instance
    `dropdown-terminal`, and the DIRECTORY must be systemd's %t (/run/user/<uid>),
    which does not follow this process's XDG_RUNTIME_DIR. If %t cannot be validated
    the validated XDG_RUNTIME_DIR is used, but loudly: silently dialing a directory
    the unit never bound would make the terminal fail with no explanation.
    """
    import focus_watcher

    systemd_dir = f"/run/user/{os.getuid()}"
    runtime = focus_watcher.runtime_dir(ancestor_uids, prefer_systemd=True)
    if runtime is None:
        raise SystemExit("no safe runtime directory for the foot socket")
    if runtime != systemd_dir:
        print(
            f"warning: foot socket in {runtime}, but the unit binds {systemd_dir} "
            "(systemd %t); if the terminal does not open, XDG_RUNTIME_DIR differs from "
            "the user manager's runtime directory",
            file=sys.stderr,
        )
    return os.path.join(runtime, FOOT_SOCKET_NAME)


# ----------------------------------------------------------------------- main


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        dispatch(cmd)
    except ConfigUnreadable as exc:
        # A single clean line, not a traceback: this is a user-facing condition.
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def dispatch(cmd):
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        status()
    elif cmd == "runtime-dir":
        print(runtime_paths()[0])
    elif cmd == "state-path":
        print(runtime_paths()[1])
    elif cmd == "socket-path":
        print(socket_path())
    elif cmd == "check-conflict":
        conflict = check_keybind_conflict()
        print(conflict or "no conflict")
        sys.exit(1 if conflict else 0)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
