#!/usr/bin/python3
"""Trusted tool resolution and bounded subprocess execution.

Everything this plugin starts on its own initiative (the focus watcher, the
backend's status/install/uninstall helpers, the CLI's hyprctl/systemctl calls)
must satisfy three rules, because enabling the widget starts it automatically:

1. The executable is resolved to an ABSOLUTE path from a fixed candidate list
   and validated - regular file, owned by root, not group/world-writable,
   executable, with every parent directory equally trusted. ``PATH`` is never
   consulted, so a malicious earlier entry in ``PATH`` cannot be launched just
   by loading the widget.
2. It runs with an explicit, minimal environment built from an allowlist
   instead of inheriting the user's whole environment. ``PATH`` is replaced
   with a controlled value covering only root-owned directories, so helpers the
   child spawns cannot resolve through a user-writable directory either.
3. Its captured output is bounded. Output is read through ``selectors`` with a
   shared byte budget; a child that exceeds it, or that outlives its timeout,
   has its process GROUP killed (SIGTERM then SIGKILL) and is reaped.

Residual, documented rather than hidden: a child that deliberately escapes its
process group (the foot client does this on purpose, via setsid, so the terminal
session survives) is not reached by the group kill; and a file could in theory
be swapped between validation and exec - that requires root, since the
candidate directories and files are root-owned and not writable by us.
"""

import os
import re
import selectors
import signal
import stat
import subprocess
import time

# Root-owned directories only. "/bin" is a symlink to /usr/bin on Arch; the
# real path is what gets validated.
CANDIDATE_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")

# The owner a tool must belong to. Parameterised (not hard-coded) so the tests
# can exercise both the accept and reject paths with fixtures they own - the
# test suite runs unprivileged and cannot create root-owned files.
TRUSTED_UID = 0

_NAME_RE = re.compile(r"\A[A-Za-z0-9._+-]{1,64}\Z")

# Everything a helper legitimately needs: session/runtime pointers, the
# compositor instance, the Wayland/X11 display, the user bus, locale and home.
ENV_ALLOW = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "HYPRLAND_INSTANCE_SIGNATURE",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
)

CONTROLLED_PATH = ":".join(CANDIDATE_DIRS)

DEFAULT_LIMIT = 256 * 1024  # shared budget for stdout+stderr, in bytes
DEFAULT_TIMEOUT = 5.0

_resolved = {}


class ToolNotFound(RuntimeError):
    """No trusted absolute path for a tool name."""


class Result:
    """What run() returns: the pieces callers actually use."""

    __slots__ = ("returncode", "stdout", "stderr", "truncated", "timed_out")

    def __init__(self, returncode, stdout, stderr, truncated=False, timed_out=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.truncated = truncated
        self.timed_out = timed_out

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"Result(rc={self.returncode}, truncated={self.truncated}, "
            f"timed_out={self.timed_out}, out={len(self.stdout)}b, err={len(self.stderr)}b)"
        )


# --------------------------------------------------------------- resolution


def _dir_trusted(path, uid=TRUSTED_UID):
    """A directory is trusted when `uid` owns it and others cannot write it."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != uid:
        return False
    return not st.st_mode & 0o022


def _ancestors_trusted(real_path, uid=TRUSTED_UID, trust_root="/"):
    """Walk from the file's directory up to the trusted root, checking each one."""
    current = os.path.dirname(real_path)
    root = os.path.realpath(trust_root)
    while True:
        if not _dir_trusted(current, uid):
            return False
        if current == root:
            return True
        parent = os.path.dirname(current)
        if parent == current:  # reached "/"
            return True
        current = parent


def resolve(name, dirs=None, uid=TRUSTED_UID, trust_root="/"):
    """Absolute path of a trusted `name`, or None. Never consults PATH.

    `dirs`, `uid` and `trust_root` default to the production values (the fixed
    root-owned candidate directories, tools owned by root, trust chain walked up
    to /) and exist so the tests can run the same logic unprivileged against
    fixtures they own.
    """
    dirs = CANDIDATE_DIRS if dirs is None else tuple(dirs)
    cache_key = (name, dirs, uid, trust_root)
    if cache_key in _resolved:
        return _resolved[cache_key]
    if not _NAME_RE.match(name or ""):
        return None
    for directory in dirs:
        try:
            real = os.path.realpath(os.path.join(directory, name))
            st = os.stat(real)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if st.st_uid != uid:
            continue
        if st.st_mode & 0o022:  # group/world writable
            continue
        if not st.st_mode & 0o111:  # not executable
            continue
        if not _ancestors_trusted(real, uid, trust_root):
            continue
        _resolved[cache_key] = real
        return real
    return None


def tool(name):
    """resolve() but raising ToolNotFound, for callers that must not continue."""
    path = resolve(name)
    if path is None:
        raise ToolNotFound(
            f"no trusted {name!r} in {' or '.join(CANDIDATE_DIRS)} "
            "(root-owned, not group/world-writable)"
        )
    return path


# --------------------------------------------------------------- execution


def build_env(base=None):
    """The minimal environment every automatic child gets."""
    source = os.environ if base is None else base
    env = {key: source[key] for key in ENV_ALLOW if source.get(key)}
    env["PATH"] = CONTROLLED_PATH
    return env


def _kill_group(proc):
    """SIGTERM then SIGKILL the child's process group, then reap it."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=0.4)
            return
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        pass


def _revalidate(argv0):
    """Re-check the executable immediately before exec (shrinks the TOCTOU window).

    Ownership is not re-checked here: argv[0] may be the plugin's own script,
    which is user-owned by design. What matters is that it is still a regular,
    executable file (or a symlink to one) at this instant.
    """
    try:
        st = os.stat(argv0)
    except OSError as exc:
        raise ValueError(f"cannot execute {argv0}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
        raise ValueError(f"refusing to execute {argv0}: not a regular executable file")
    return True


def run(argv, timeout=DEFAULT_TIMEOUT, limit=DEFAULT_LIMIT, env=None, cwd=None):
    """Run an absolute-path argv array with a minimal env and bounded output.

    Raises ValueError for a non-absolute argv[0] (which would otherwise be
    resolved through PATH by the OS) and ToolNotFound/OSError for a broken
    executable. Returns a Result; never returns unbounded output, and never
    leaves a killed child unreaped.
    """
    if not argv or not isinstance(argv[0], str) or not os.path.isabs(argv[0]):
        raise ValueError(
            "run() requires an absolute argv[0] resolved through resolve()/tool()"
        )
    _revalidate(argv[0])

    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=build_env() if env is None else env,
        cwd=cwd,
        start_new_session=True,
        close_fds=True,
    )

    out_pipe = proc.stdout
    err_pipe = proc.stderr
    if out_pipe is None or err_pipe is None:  # pragma: no cover - PIPE was requested
        _kill_group(proc)
        raise OSError("subprocess pipes unavailable")

    selector = selectors.DefaultSelector()
    selector.register(out_pipe, selectors.EVENT_READ, "stdout")
    selector.register(err_pipe, selectors.EVENT_READ, "stderr")

    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    truncated = False
    timed_out = False
    deadline = time.monotonic() + max(0.1, float(timeout))

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, 0.5)):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                room = limit - total
                if room <= 0:
                    truncated = True
                    break
                if len(data) > room:
                    data = data[:room]
                    truncated = True
                chunks[key.data] += data
                total += len(data)
                if truncated:
                    break
            if truncated:
                break
    finally:
        selector.close()
        if truncated or timed_out:
            _kill_group(proc)
        else:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
        for stream in (out_pipe, err_pipe):
            try:
                stream.close()
            except OSError:  # pragma: no cover
                pass

    return Result(
        proc.returncode,
        chunks["stdout"].decode("utf-8", "replace"),
        chunks["stderr"].decode("utf-8", "replace"),
        truncated=truncated,
        timed_out=timed_out,
    )
