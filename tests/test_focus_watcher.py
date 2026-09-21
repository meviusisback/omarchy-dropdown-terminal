#!/usr/bin/python3
"""Tests for backend/focus_watcher.py: event state machine and path validation.

The state machine is driven directly with Hyprland-style event lines (the exact
wire format was captured from a live 0.56.2 session), so the dismissal logic is
covered without a compositor.
"""

import json
import os
import pwd
import socket
import stat
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

import focus_watcher as watcher_mod  # noqa: E402

APP = watcher_mod.APP_ID
WS = watcher_mod.SPECIAL_WS


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.w = watcher_mod.Watcher()

    def test_focus_in_then_out_hides_once(self):
        self.assertIsNone(self.w.feed(f"activewindow>>{APP},alberto@omarchy:~"))
        self.assertTrue(self.w.focused)
        self.assertIsNone(self.w.feed("activewindow>>brave-origin,Some Page"))
        self.assertFalse(self.w.focused)
        # a later unrelated focus change must not hide again
        self.assertIsNone(self.w.feed("activewindow>>foot,term"))

    def test_hide_requires_visible(self):
        self.w.feed(f"activewindow>>{APP},shell")
        self.assertFalse(self.w.visible)
        self.assertIsNone(self.w.feed("activewindow>>foot,x"))

    def test_hide_fires_when_visible(self):
        self.w.feed(f"activespecial>>{WS},HDMI-A-1")
        self.assertTrue(self.w.visible)
        self.w.feed(f"activewindow>>{APP},shell")
        self.assertEqual(self.w.feed("activewindow>>foot,x"), "hide")
        # and only once: the dropdown is no longer the focused window
        self.assertIsNone(self.w.feed("activewindow>>foot,x"))

    def test_never_focused_dropdown_does_not_hide(self):
        self.w.feed(f"activespecial>>{WS},HDMI-A-1")
        self.assertEqual(self.w.feed("activewindow>>foot,x"), None)

    def test_activespecial_show_and_hide(self):
        self.assertEqual(self.w.feed(f"activespecial>>{WS},HDMI-A-1"), "state")
        self.assertTrue(self.w.visible)
        self.assertEqual(self.w.feed("activespecial>>,HDMI-A-1"), "state")
        self.assertFalse(self.w.visible)

    def test_repeated_visibility_reports_no_change(self):
        # Only a real change may return "state": the degraded poll feeds the same
        # activespecial line every 2 s, and rewriting the file 43k times a day would
        # both waste work and keep waking the widget's FileView.
        self.assertEqual(self.w.feed(f"activespecial>>{WS},HDMI-A-1"), "state")
        self.assertIsNone(self.w.feed(f"activespecial>>{WS},HDMI-A-1"))
        self.assertIsNone(self.w.feed(f"activespecialv2>>-98,{WS},HDMI-A-1"))
        self.assertEqual(self.w.feed("activespecial>>,HDMI-A-1"), "state")
        self.assertIsNone(self.w.feed("activespecial>>,HDMI-A-1"))

    def test_activespecialv2_prefixes_the_id(self):
        self.w.feed(f"activespecialv2>>-98,{WS},HDMI-A-1")
        self.assertTrue(self.w.visible)
        self.w.feed("activespecialv2>>0,,HDMI-A-1")
        self.assertFalse(self.w.visible)

    def test_lookalike_class_is_not_us(self):
        self.w.feed(f"activespecial>>{WS},HDMI-A-1")
        self.w.feed(f"activewindow>>{APP},shell")
        self.assertEqual(self.w.feed(f"activewindow>>{APP}.evil,spoofed"), "hide")

    def test_title_with_separator_characters_is_harmless(self):
        self.assertIsNone(self.w.feed(f"activewindow>>{APP},title,with,commas>>and arrows"))
        self.assertTrue(self.w.focused)

    def test_oversized_line_is_dropped(self):
        self.w.feed(f"activewindow>>{APP},shell")
        oversized = "activewindow>>foot," + "x" * 5000
        self.assertIsNone(self.w.feed(oversized))
        self.assertTrue(self.w.focused)  # state untouched by the oversized line

    def test_malformed_lines_are_ignored(self):
        for line in ("", "garbage", "no-separator", ">>", "\n", b"\x00\x01\x02"):
            self.assertIsNone(self.w.feed(line))
        self.assertFalse(self.w.focused)
        self.assertFalse(self.w.visible)

    def test_unknown_event_is_ignored(self):
        self.assertIsNone(self.w.feed("openwindow>>1234,1,foot,term"))
        self.assertIsNone(self.w.feed("workspace>>2"))


class RuntimeDirTest(unittest.TestCase):
    """runtime_dir() validation.

    `sandbox_uids` is injected because this suite also runs inside a bubblewrap
    user namespace where root-owned paths read as uid 65534 (uid 0 unmapped), so the
    ancestor walk would reject every real directory there. Production passes nothing
    and therefore trusts only root and the current user.
    """

    SANDBOX_UIDS = (0, os.getuid(), os.stat("/").st_uid)

    def test_accepts_the_valid_session_dir(self):
        with _env("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"):
            self.assertEqual(
                watcher_mod.runtime_dir(self.SANDBOX_UIDS), f"/run/user/{os.getuid()}"
            )

    def test_rejects_relative_world_writable_and_root_paths(self):
        for bad in ("tmp", "/tmp", "", "/etc", "/usr"):
            with _env("XDG_RUNTIME_DIR", bad):
                # falls back to the real per-user dir, which is 0700 and ours
                self.assertEqual(
                    watcher_mod.runtime_dir(self.SANDBOX_UIDS), f"/run/user/{os.getuid()}"
                )

    def test_rejects_a_private_dir_inside_a_world_writable_parent(self):
        # A 0700 directory whose name sits in a world-writable parent can be
        # replaced: the leaf check alone is not enough, so this must be refused.
        parent = tempfile.mkdtemp(prefix="ddt-parent-")
        os.chmod(parent, 0o777)
        child = os.path.join(parent, "rt")
        os.mkdir(child, 0o700)
        with _env("XDG_RUNTIME_DIR", child):
            self.assertEqual(
                watcher_mod.runtime_dir(self.SANDBOX_UIDS), f"/run/user/{os.getuid()}"
            )

    def test_rejects_home_and_its_ancestors(self):
        home = os.path.expanduser("~")
        for bad in (home, os.path.dirname(home), "/"):
            with _env("XDG_RUNTIME_DIR", bad):
                self.assertEqual(
                    watcher_mod.runtime_dir(self.SANDBOX_UIDS), f"/run/user/{os.getuid()}"
                )

    def test_home_guard_survives_an_empty_home(self):
        # expanduser("~") returns "/" for an empty HOME, which silently disabled the
        # HOME/ancestor refusal; the passwd entry must be used instead.
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        with _env("HOME", ""):
            self.assertEqual(watcher_mod.home_dir(), real_home)
        with _env("HOME", "relative/home"):
            self.assertEqual(watcher_mod.home_dir(), real_home)

    def test_returns_none_when_nothing_is_acceptable(self):
        # The leaf is always required to be owned by the REAL uid (ancestor_uids only
        # constrains the directories above it), so the "nothing qualifies" case needs a
        # uid that owns no candidate at all: /etc is root-owned and /run/user/<uid> does
        # not exist for it, so neither the XDG candidate nor the systemd fallback passes.
        with _env("XDG_RUNTIME_DIR", "/etc"), mock.patch.object(
            os, "getuid", return_value=424242
        ):
            self.assertIsNone(watcher_mod.runtime_dir((0, 424242)))


class SocketPathTest(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix="ddt-rt-")
        os.chmod(self.runtime, 0o700)
        self.signature = "test_1234_5678"

    def _socket(self, signature=None):
        signature = signature or self.signature
        directory = os.path.join(self.runtime, "hypr", signature)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, ".socket2.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        self.addCleanup(sock.close)
        return path

    def test_accepts_a_real_owned_socket(self):
        path = self._socket()
        with _env("HYPRLAND_INSTANCE_SIGNATURE", self.signature):
            self.assertEqual(watcher_mod.socket_path(self.runtime), path)

    def test_rejects_planted_regular_file(self):
        directory = os.path.join(self.runtime, "hypr", self.signature)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, ".socket2.sock"), "w") as handle:
            handle.write("forged\n")
        with _env("HYPRLAND_INSTANCE_SIGNATURE", self.signature):
            self.assertIsNone(watcher_mod.socket_path(self.runtime))

    def test_rejects_traversal_and_odd_signatures(self):
        for bad in ("..", "../../etc", "a/b", "with space", "x" * 200, ""):
            with _env("HYPRLAND_INSTANCE_SIGNATURE", bad):
                self.assertIsNone(
                    watcher_mod.socket_path(self.runtime), f"signature {bad!r}"
                )

    def test_rejects_missing_socket(self):
        with _env("HYPRLAND_INSTANCE_SIGNATURE", self.signature):
            self.assertIsNone(watcher_mod.socket_path(self.runtime))

    def test_accepts_a_live_listening_socket(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(os.path.join(self.runtime, "live.sock"))
        listener.listen(1)
        self.assertTrue(watcher_mod.can_connect(os.path.join(self.runtime, "live.sock")))

    def test_rejects_a_stale_socket_file_with_no_listener(self):
        # A socket left behind by a restarted compositor: stat() is happy, but
        # nothing accepts, so the watcher must not keep retrying the event path.
        stale = os.path.join(self.runtime, "stale.sock")
        temp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        temp.bind(stale)
        temp.close()
        self.assertFalse(watcher_mod.can_connect(stale, timeout=0.2))

    def test_socket_path_is_never_a_symlink(self):
        # lstat, not stat: the CLI rejects a symlink at the foot socket, so the
        # watcher must not accept one at the event socket either.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        elsewhere = os.path.join(self.runtime, "real.sock")
        sock.bind(elsewhere)
        self.addCleanup(sock.close)
        sig_dir = os.path.join(self.runtime, "hypr", self.signature)
        os.makedirs(sig_dir, exist_ok=True)
        os.symlink(elsewhere, os.path.join(sig_dir, ".socket2.sock"))
        with _env("HYPRLAND_INSTANCE_SIGNATURE", self.signature):
            self.assertIsNone(watcher_mod.socket_path(self.runtime))

    def test_rejects_symlinked_signature_directory(self):
        # A symlinked component would let the subscription be redirected.
        elsewhere = tempfile.mkdtemp(prefix="ddt-elsewhere-")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(os.path.join(elsewhere, ".socket2.sock"))
        self.addCleanup(sock.close)
        os.makedirs(os.path.join(self.runtime, "hypr"), exist_ok=True)
        os.symlink(elsewhere, os.path.join(self.runtime, "hypr", self.signature))
        with _env("HYPRLAND_INSTANCE_SIGNATURE", self.signature):
            self.assertIsNone(watcher_mod.socket_path(self.runtime))


class ToolCallGuardTest(unittest.TestCase):
    """Failed probes are 'no information', never a value, never a hide."""

    def setUp(self):
        self._real_run = watcher_mod.proc.run
        self.addCleanup(lambda: setattr(watcher_mod.proc, "run", self._real_run))

    def _run_returns(self, result):
        watcher_mod.proc.run = lambda *args, **kwargs: result

    def test_failed_command_is_none_not_false(self):
        self._run_returns(watcher_mod.proc.Result(1, "", "boom"))
        self.assertIsNone(watcher_mod.is_visible("/usr/bin/hyprctl"))
        self.assertIsNone(watcher_mod.active_class("/usr/bin/hyprctl"))

    def test_malformed_output_is_none(self):
        self._run_returns(watcher_mod.proc.Result(0, "not json", ""))
        self.assertIsNone(watcher_mod.is_visible("/usr/bin/hyprctl"))
        self.assertIsNone(watcher_mod.active_class("/usr/bin/hyprctl"))

    def test_raising_run_is_folded_into_none(self):
        def boom(*args, **kwargs):
            raise ValueError("bad argv")

        watcher_mod.proc.run = boom
        self.assertIsNone(watcher_mod.is_visible("/usr/bin/hyprctl"))
        self.assertIsNone(watcher_mod.active_class("/usr/bin/hyprctl"))

    def test_good_probes_report_their_value(self):
        self._run_returns(watcher_mod.proc.Result(
            0, json.dumps([{"specialWorkspace": {"name": watcher_mod.SPECIAL_WS}}]), ""))
        self.assertTrue(watcher_mod.is_visible("/usr/bin/hyprctl"))
        self._run_returns(watcher_mod.proc.Result(
            0, json.dumps([{"specialWorkspace": None}]), ""))
        self.assertFalse(watcher_mod.is_visible("/usr/bin/hyprctl"))
        self._run_returns(watcher_mod.proc.Result(0, json.dumps({"class": "foot"}), ""))
        self.assertEqual(watcher_mod.active_class("/usr/bin/hyprctl"), "foot")

    def test_parseable_but_wrong_shape_is_no_information(self):
        # "null"/"5" are valid JSON and were iterated straight into a TypeError that
        # killed the watcher; {} and [1,2] were folded back into the "hidden"
        # sentinel this code exists to avoid. The nested field must be checked too:
        # a truthy non-dict specialWorkspace used to raise AttributeError.
        for payload in ("null", "5", "true", "{}", '{"a": 1}', "[1, 2]", "[null]",
                        '[{"specialWorkspace": 5}]', '[{"specialWorkspace": "x"}]',
                        '[{"specialWorkspace": []}]', '[{"specialWorkspace": 1.5}]'):
            self._run_returns(watcher_mod.proc.Result(0, payload, ""))
            self.assertIsNone(watcher_mod.is_visible("/usr/bin/hyprctl"), payload)

    def test_empty_monitor_list_is_hidden_not_unknown(self):
        # A well-formed but empty list is a compositor with nothing shown, not a
        # failed probe: folding it into None would stop the state file updating.
        self._run_returns(watcher_mod.proc.Result(0, "[]", ""))
        self.assertFalse(watcher_mod.is_visible("/usr/bin/hyprctl"))

    def test_poll_tick_never_hides_on_a_failed_probe(self):
        # Regression: the degraded tick used to feed the empty-string sentinel as a
        # real activewindow change, so one failed hyprctl call while the terminal was
        # focused and visible hid it mid-typing.
        hidden = []
        real_hide = watcher_mod.hide_dropdown
        watcher_mod.hide_dropdown = lambda: hidden.append(True)
        self.addCleanup(lambda: setattr(watcher_mod, "hide_dropdown", real_hide))
        watcher = watcher_mod.Watcher()
        watcher.visible = True
        watcher.focused = True
        self._run_returns(watcher_mod.proc.Result(1, "", "hyprctl unavailable"))
        watcher_mod.poll_tick(watcher, os.path.join(tempfile.mkdtemp(), "state"), "/usr/bin/hyprctl")
        self.assertEqual(hidden, [])
        self.assertTrue(watcher.visible)
        self.assertTrue(watcher.focused)

    def test_run_tool_folds_tool_not_found(self):
        def missing(*args, **kwargs):
            raise watcher_mod.proc.ToolNotFound("nope")

        original = watcher_mod.proc.run
        watcher_mod.proc.run = missing
        try:
            result = watcher_mod.run_tool(["/usr/bin/hyprctl", "monitors", "-j"], timeout=1)
            self.assertNotEqual(result.returncode, 0)
        finally:
            watcher_mod.proc.run = original


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix="ddt-state-")
        os.chmod(self.runtime, 0o700)
        self.path = os.path.join(self.runtime, watcher_mod.STATE_NAME)

    def test_handle_skips_state_write_when_runtime_dir_changed(self):
        # The runtime dir was swapped after startup validation: publishing into
        # the unvalidated tree would desync the widget, so the write is skipped.
        watcher = watcher_mod.Watcher()
        watcher.visible = True
        real_runtime_dir = watcher_mod.runtime_dir
        watcher_mod.runtime_dir = lambda: "/elsewhere-now"
        self.addCleanup(lambda: setattr(watcher_mod, "runtime_dir", real_runtime_dir))
        logged = []
        real_log = watcher_mod.log
        watcher_mod.log = logged.append
        self.addCleanup(lambda: setattr(watcher_mod, "log", real_log))
        watcher_mod.handle(watcher, self.path, "state", rt=self.runtime)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(any("skipping state write" in line for line in logged))

    def test_handle_writes_when_runtime_dir_unchanged(self):
        watcher = watcher_mod.Watcher()
        watcher.visible = True
        real_runtime_dir = watcher_mod.runtime_dir
        watcher_mod.runtime_dir = lambda: self.runtime
        self.addCleanup(lambda: setattr(watcher_mod, "runtime_dir", real_runtime_dir))
        watcher_mod.handle(watcher, self.path, "state", rt=self.runtime)
        with open(self.path) as handle:
            self.assertIs(json.load(handle)["visible"], True)

    def test_writes_json_at_0600_without_leftovers(self):
        self.assertTrue(watcher_mod.write_state(self.path, True))
        with open(self.path) as handle:
            payload = json.load(handle)
        self.assertIs(payload["visible"], True)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(
            [n for n in os.listdir(self.runtime) if n.startswith(".ddterm-state-")], []
        )

    def test_overwrites_previous_state(self):
        watcher_mod.write_state(self.path, True)
        watcher_mod.write_state(self.path, False)
        with open(self.path) as handle:
            self.assertIs(json.load(handle)["visible"], False)

    def test_unwritable_directory_is_reported_not_raised(self):
        os.chmod(self.runtime, 0o500)
        try:
            self.assertFalse(watcher_mod.write_state(self.path, True))
        finally:
            os.chmod(self.runtime, 0o700)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix="ddt-lock-")

    def test_lock_is_0600_and_exclusive(self):
        fd = watcher_mod.acquire_lock(self.runtime)
        self.addCleanup(os.close, fd)
        path = os.path.join(self.runtime, watcher_mod.LOCK_NAME)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # a second holder (separate open file description) must be refused
        self.assertRaises(OSError, watcher_mod.acquire_lock, self.runtime)

    def test_symlinked_lock_path_is_refused(self):
        target = os.path.join(self.runtime, "victim")
        with open(target, "w") as handle:
            handle.write("important\n")
        os.symlink(target, os.path.join(self.runtime, watcher_mod.LOCK_NAME))
        self.assertRaises(OSError, watcher_mod.acquire_lock, self.runtime)
        with open(target) as handle:
            self.assertEqual(handle.read(), "important\n")


def _env(name, value):
    """Context manager: set an environment variable for the block."""
    class _Ctx:
        def __enter__(self):
            self.previous = os.environ.get(name)
            os.environ[name] = value
            return value

        def __exit__(self, *exc):
            if self.previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = self.previous
            return False

    return _Ctx()


if __name__ == "__main__":
    unittest.main(verbosity=2)
