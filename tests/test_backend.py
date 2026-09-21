#!/usr/bin/python3
"""Tests for the dropdown-terminal backend: idempotency, atomicity, uninstall.

Hermetic by construction: HOME is repointed at a temp directory before the module
is imported, and proc.run / proc.resolve are substituted in setUp, so a test run
never executes systemctl against the developer's user manager and never depends on
the environment's real tool ownership.
"""

import importlib
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

SANDBOX = tempfile.mkdtemp(prefix="ddt-test-")
os.environ["HOME"] = SANDBOX
# XDG_CONFIG_HOME decides where the config lives (Hyprland reads it), so drop it:
# it would otherwise point at the developer's REAL config directory (it did, and the
# read-only sandbox root is what stopped the suite writing there). Falling back to
# $HOME/.config then keeps every path inside this sandbox.
os.environ.pop("XDG_CONFIG_HOME", None)
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

# Uids the ancestor walk may trust in this environment: the dev sandbox maps uid 0
# to 65534 (unmapped in the user namespace), so tests inject that too.
TEST_UIDS = (0, os.getuid(), os.stat("/").st_uid)

backend = importlib.import_module("dropdown_terminal")


def terminal_run_bash(script):
    """Run a shell snippet and return its combined output.

    Used to drive the CLI's own functions with stubbed tools, which is how the
    socket-readiness branch is kept honest (it is dead code if an early return
    creeps back in).
    """
    completed = subprocess.run(
        ["/usr/bin/bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    return completed.stdout + completed.stderr


class BackendTest(unittest.TestCase):
    def setUp(self):
        for p in (backend.HYPRLAND_LUA, backend.BINDINGS_LUA, backend.RULES_PATH,
                  backend.UNIT_DST_PATH, backend.UNIT_DST_PATH + ".pre-dropdown-terminal.bak"):
            if os.path.exists(p):
                os.unlink(p)

        # Hermetic substitution: tools "resolve" without touching the filesystem's
        # real ownership, and proc.run is replaced by a synthetic success so no test
        # can start systemctl against this machine's user manager. _call() itself
        # records every argv in backend.CALLS.
        self._real_run = backend.proc.run
        self._real_resolve = backend.proc.resolve
        backend.CALLS.clear()
        backend.proc.resolve = lambda name, *args, **kwargs: f"/usr/bin/{name}"
        backend.proc.run = self._fake_run

        def restore():
            backend.proc.run = self._real_run
            backend.proc.resolve = self._real_resolve

        self.addCleanup(restore)

    @staticmethod
    def _fake_run(argv, timeout=None, limit=None, env=None, cwd=None):
        """Stand-in for proc.run: reports success without starting anything."""
        return backend.proc.Result(0, "", "")

    def test_rules_body_has_markers(self):
        self.assertIn(backend.RULES_BEGIN, backend.RULES_BODY)
        self.assertIn(backend.RULES_END, backend.RULES_BODY)
        self.assertIn('o.window("org.omarchy.dropdown-terminal"', backend.RULES_BODY)
        self.assertIn("special:dropdown", backend.RULES_BODY)
        self.assertIn("float = true", backend.RULES_BODY)

    def test_rules_body_must_not_pin_or_steal_focus(self):
        # Regression: pin=true + stay_focused=true let a window that spawned
        # while the special workspace was hidden become an always-on-top
        # overlay on every workspace that no toggle could hide (real trap on
        # the live host). The generated rules must never contain them.
        self.assertNotIn("pin = true", backend.RULES_BODY)
        self.assertNotIn("stay_focused = true", backend.RULES_BODY)

    def test_rules_body_pins_the_drop_direction(self):
        # Regression: a bare `slidevert` keeps Hyprland's default for special
        # workspaces, which starts one screen BELOW on IN (Monitor.cpp passes
        # left = true) - the panel rose from the bottom. The direction token in
        # the style string overrides that, so both leaves must carry one, and
        # the out leaf must retract the way it came. Matched per animation call
        # (not as one literal line) so reformatting RULES_BODY cannot fail a
        # behaviour-neutral test.
        for leaf, style in (
            ("specialWorkspaceIn", "slidevert top"),
            ("specialWorkspaceOut", "slidevert bottom"),
        ):
            pattern = (
                r"hl\.animation\(\{[^}]*leaf = \"" + leaf + r"\"[^}]*"
                r"style = \"" + style + r"\"[^}]*\}\)"
            )
            self.assertRegex(
                backend.RULES_BODY,
                pattern,
                f"{leaf} must pin its drop direction ({style})",
            )

    def test_bind_block_roundtrip(self):
        backend.atomic_write(backend.BINDINGS_LUA, "-- my binds\n")
        body = backend.bind_body()
        self.assertTrue(backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, body, backend.BIND_END))
        content = backend.read_text(backend.BINDINGS_LUA)
        self.assertIn('o.bind("SUPER + U"', content)
        self.assertIn("-- my binds", content)

        # Idempotent: second append is a no-op, no duplication.
        self.assertFalse(backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, body, backend.BIND_END))
        self.assertEqual(backend.read_text(backend.BINDINGS_LUA).count("o.bind("), 1)

    def test_append_block_keeps_the_marker_on_its_own_line(self):
        # The block must be three lines. Gluing the body and the END marker onto one
        # line still parses as Lua (the marker turns into a comment) but is not what
        # bind_line() writes, and remove_block() then drops the body with the marker.
        backend.atomic_write(backend.BINDINGS_LUA, "-- my binds\n")
        body = backend.bind_body()
        self.assertTrue(
            backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, body, backend.BIND_END)
        )
        lines = backend.read_text(backend.BINDINGS_LUA).splitlines()
        self.assertIn(body, lines, "the body must sit alone on its line")
        self.assertIn(backend.BIND_END, lines, "the END marker must not share the body's line")

    def test_append_block_normalises_a_glued_block(self):
        # Shape written by the old append path: body and END marker on one line.
        backend.atomic_write(
            backend.BINDINGS_LUA,
            "-- my binds\n"
            + backend.BIND_BEGIN
            + "\n"
            + backend.bind_body()
            + backend.BIND_END
            + "\n",
        )
        self.assertTrue(
            backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, backend.bind_body(), backend.BIND_END)
        )
        lines = backend.read_text(backend.BINDINGS_LUA).splitlines()
        self.assertIn(backend.bind_body(), lines)
        self.assertIn(backend.BIND_END, lines)
        self.assertIn("-- my binds", lines, "content outside the block is preserved")
        self.assertFalse(
            backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, backend.bind_body(), backend.BIND_END),
            "the canonical shape must be a no-op on the next run",
        )

    def test_install_rewrites_a_stale_keybind_body_and_reports_it(self):
        # The marker proves the block exists; the body inside it is what Hyprland runs.
        # An older install wrote a bare command name - a reinstall must replace it and
        # say "updated" instead of reporting the marker as already-installed.
        stale_body = backend.bind_body().replace(
            f"{backend.HOME}/.local/bin/omarchy-dropdown-terminal", "omarchy-dropdown-terminal"
        )
        self.assertNotEqual(stale_body, backend.bind_body())
        backend.atomic_write(
            backend.BINDINGS_LUA,
            "-- my binds\n"
            + backend.BIND_BEGIN
            + "\n"
            + stale_body
            + "\n"
            + backend.BIND_END
            + "\n",
        )
        results = backend.install(quiet=True)
        self.assertEqual(results["keybind"], "updated")
        content = backend.read_text(backend.BINDINGS_LUA)
        self.assertIn(backend.bind_body(), content)
        self.assertNotIn('"omarchy-dropdown-terminal toggle"', content)
        self.assertEqual(backend.install(quiet=True)["keybind"], "already-installed")

    def test_remove_block(self):
        backend.atomic_write(backend.BINDINGS_LUA, "before\n" + backend.bind_line() + "after\n")
        self.assertTrue(backend.remove_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, backend.BIND_END))
        content = backend.read_text(backend.BINDINGS_LUA)
        self.assertIn("before", content)
        self.assertIn("after", content)
        self.assertNotIn("o.bind(", content)

    def test_remove_block_unterminated_raises(self):
        backend.atomic_write(backend.BINDINGS_LUA, backend.BIND_BEGIN + "\nno end marker\n")
        with self.assertRaises(ValueError):
            backend.remove_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, backend.BIND_END)

    def test_unit_install_backs_up_foreign_file(self):
        os.makedirs(backend.SYSTEMD_USER_DIR, exist_ok=True)
        backend.atomic_write(backend.UNIT_DST_PATH, "# my precious custom unit\n")
        backend.install_unit()
        backup = backend.UNIT_DST_PATH + ".pre-dropdown-terminal.bak"
        self.assertTrue(os.path.exists(backup))
        with open(backup) as f:
            self.assertEqual(f.read(), "# my precious custom unit\n")
        with open(backend.UNIT_DST_PATH) as f:
            self.assertIn(backend.MARKER, f.read())

    def test_unit_install_idempotent_no_second_backup(self):
        backend.install_unit()
        backend.install_unit()
        self.assertFalse(os.path.exists(backend.UNIT_DST_PATH + ".pre-dropdown-terminal.bak"))

    def test_hook_line_install_and_removal(self):
        backend.atomic_write(backend.HYPRLAND_LUA, 'require("hypr.bindings")\n')
        backend.install(quiet=True)
        content = backend.read_text(backend.HYPRLAND_LUA)
        self.assertIn("dropdown-terminal.lua", content)
        self.assertIn("dofile(path)", content)
        self.assertIn('require("hypr.bindings")', content)  # untouched

        self.assertTrue(backend._remove_hook_line())
        content = backend.read_text(backend.HYPRLAND_LUA)
        self.assertNotIn("dropdown-terminal", content)
        self.assertIn('require("hypr.bindings")', content)

    def test_conflict_detection(self):
        backend.atomic_write(
            backend.BINDINGS_LUA,
            'o.bind("SUPER + U", "Something else", "echo hi")\n',
        )
        conflict = backend.check_keybind_conflict()
        self.assertIsNotNone(conflict)
        self.assertIn("Something else", conflict)

    def test_no_conflict_with_own_block(self):
        backend.atomic_write(backend.BINDINGS_LUA, backend.bind_line())
        self.assertIsNone(backend.check_keybind_conflict())

    def test_atomic_write_preserves_mode(self):
        os.chmod(backend.BINDINGS_LUA, 0o600) if os.path.exists(backend.BINDINGS_LUA) else None
        backend.atomic_write(backend.BINDINGS_LUA, "x\n")
        os.chmod(backend.BINDINGS_LUA, 0o600)
        backend.atomic_write(backend.BINDINGS_LUA, "y\n")
        self.assertEqual(os.stat(backend.BINDINGS_LUA).st_mode & 0o777, 0o600)

    def test_no_temp_leftovers(self):
        backend.install(quiet=True)
        for name in os.listdir(backend.HYPRLAND_DIR):
            self.assertFalse(name.startswith(".ddterm-"), f"leftover temp file: {name}")

    def test_every_external_call_uses_an_absolute_trusted_path(self):
        # The plugin is started automatically once the widget is enabled, so no
        # call site may leave a tool to be found through PATH. setUp() substitutes
        # proc.resolve with a stub (the dev sandbox cannot see root-owned files) and
        # _call() records every argv it builds.
        backend.install(quiet=True)
        backend.uninstall()
        self.assertTrue(backend.CALLS, "expected the install path to call systemctl")
        for argv in backend.CALLS:
            self.assertTrue(os.path.isabs(argv[0]), argv)
            self.assertTrue(
                any(argv[0].startswith(directory + os.sep)
                    for directory in backend.proc.CANDIDATE_DIRS),
                f"{argv[0]} does not come from a trusted candidate directory",
            )

    def test_installed_unit_uses_the_resolved_foot_path(self):
        # ExecStart must be the binary that was validated, not a hardcoded path.
        backend.proc.resolve = lambda name, *args, **kwargs: f"/opt/verified/{name}"
        backend.install(quiet=True)
        with open(backend.UNIT_DST_PATH) as handle:
            unit = handle.read()
        self.assertIn("ExecStart=/opt/verified/foot --server=%t/foot-%i.sock", unit)
        self.assertNotIn("ExecStart=/usr/bin/foot", unit)

    def test_no_call_site_passes_a_bare_tool_name(self):
        # A bare name in an argv array is resolved through PATH by the OS, which
        # is exactly what the review blocked. _call() must be the only way the
        # backend starts a tool.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "backend", "dropdown_terminal.py")) as handle:
            source = handle.read()
        self.assertNotIn("_run([\"", source)
        self.assertNotIn("subprocess.", source)

    def test_install_fails_loudly_without_trusted_tools(self):
        backend.proc.resolve = lambda *args, **kwargs: None
        with self.assertRaises(SystemExit) as caught:
            backend.install(quiet=True)
        self.assertIn("missing trusted tools", str(caught.exception))

    def test_backend_has_no_environment_switched_test_mode(self):
        # A test seam that switches off real behaviour via an environment variable
        # is environment-trusted: a stray DDT_TEST=1 would make install/uninstall
        # report success while doing nothing. The suite substitutes proc.run
        # instead, so the library must not consult such a flag.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "backend", "dropdown_terminal.py")) as handle:
            source = handle.read()
        self.assertNotIn("DDT_TEST", source)

    def test_plugin_scripts_pin_absolute_interpreters(self):
        # A `#!/usr/bin/env …` shebang is a PATH lookup performed by the kernel,
        # which would reintroduce exactly what the review asked to remove.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        for rel in ("backend/dropdown_terminal.py", "backend/focus_watcher.py",
                    "backend/proc.py", "bin/omarchy-dropdown-terminal", "install.sh"):
            with open(os.path.join(repo, rel)) as handle:
                first = handle.readline().strip()
            self.assertRegex(first, r"^#!/usr/bin/(python3|bash)$", rel)

    def test_panel_launches_every_automatic_process_through_the_cli(self):
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "Panel.qml")) as handle:
            panel = handle.read()
        # No bare interpreter: the watcher runs through the CLI's own subcommand,
        # which resolves the interpreter itself.
        self.assertNotIn('["python3"', panel)
        self.assertIn('root.cliArgv(["watcher"])', panel)
        self.assertIn('root.cliArgv(["status"])', panel)
        # The state path is asked for, never derived from a raw env variable.
        self.assertIn('root.cliArgv(["state-path"])', panel)
        self.assertNotIn('Quickshell.env("XDG_RUNTIME_DIR") + "/dropdown-terminal.state"', panel)
        # Every automatic process clears the environment first, PATH included.
        self.assertIn('"/usr/bin/env"', panel)
        self.assertIn('"-i"', panel)
        self.assertNotIn('"PATH=/usr/bin', panel)
        # The isolated-interpreter flags live where the interpreter is resolved.
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            cli = handle.read()
        self.assertIn('exec "$PYTHON" -I -E -S', cli)

    def test_runtime_dir_rules_live_in_one_place(self):
        # The CLI and the widget must not re-implement the runtime-dir rules: they
        # ask the backend, whose focus_watcher.runtime_dir() is the only validator.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            cli = handle.read()
        self.assertIn("run_backend socket-path", cli)
        self.assertIn("run_backend state-path", cli)
        self.assertNotIn('/run/user/$UID', cli)      # no second fallback rule
        # the real uid comes from the kernel, not from a settable variable
        self.assertIn("-L -c '%u' -- /proc/self", cli)
        runtime, state = backend.runtime_paths(TEST_UIDS)
        self.assertTrue(runtime.startswith("/"))
        self.assertEqual(state, os.path.join(runtime, "dropdown-terminal.state"))

    def test_oversized_config_is_never_clobbered(self):
        # read_text() used to report a present-but-unreadable/oversized file as
        # "absent", and install() then overwrote the user's Hyprland config with our
        # marker block. It must refuse loudly instead.
        big = backend.HYPRLAND_LUA
        os.makedirs(os.path.dirname(big), exist_ok=True)
        os.makedirs(backend.SYSTEMD_USER_DIR, exist_ok=True)
        payload = "-- user config\n" + ("x" * (backend.MAX_CONFIG_BYTES + 10))
        with open(big, "w") as handle:
            handle.write(payload)
        with open(backend.BINDINGS_LUA, "w") as handle:
            handle.write(payload)

        with self.assertRaises(backend.ConfigUnreadable):
            backend.install(quiet=True)
        with open(big) as handle:
            self.assertEqual(handle.read(), payload)      # untouched
        with open(backend.BINDINGS_LUA) as handle:
            self.assertEqual(handle.read(), payload)

    def test_missing_config_is_still_absent(self):
        read = backend.read_text(os.path.join(SANDBOX, "definitely-not-here"))
        self.assertIsNone(read)

    def test_runtime_paths_fails_loudly_without_a_safe_directory(self):
        # No acceptable runtime dir must be an error, not a silent fallback to
        # somewhere a local attacker could plant the state file or the socket.
        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/etc"}):
            with self.assertRaises(SystemExit) as caught:
                backend.runtime_paths(())
        self.assertIn("no safe runtime directory", str(caught.exception))

    def test_status_output_is_bounded_and_validated(self):
        # The widget reads this JSON as text with no size cap of its own, so the
        # backend must not echo an oversized or non-hex "address" back to it.
        huge = "0x" + "a" * 4096
        payload = json.dumps([{
            "class": backend.DROPDOWN_APP_ID,
            "address": huge,
            "workspace": {"name": "w" * 4096},
        }])
        original = backend._call

        def fake_call(name, *args, timeout=10, **kwargs):
            if name == "hyprctl" and args[:1] == ("clients",):
                return backend.proc.Result(0, payload, "")
            return backend.proc.Result(0, "active", "")

        backend._call = fake_call
        try:
            out = backend.status()
        finally:
            backend._call = original
        self.assertIsNone(out["window"])          # not the compositor's hex shape
        self.assertLessEqual(len(out["workspace"]), 64)

    def test_cli_resolves_every_tool_its_commands_use(self):
        # Regression: `close` calls pull_window_into_special -> unpin_if_pinned,
        # which uses $SLEEP; under `set -u` an unresolved variable aborts the whole
        # command before the hide, so every reachable tool must be listed.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            cli = handle.read()
        self.assertIn('close)       wanted="hyprctl python3 sleep"', cli)
        self.assertIn('uninstall)   wanted="hyprctl python3 systemctl rm"', cli)
        # and the variables those lists resolve are the ones actually used
        self.assertIn('"$SLEEP" 0.2', cli)
        self.assertIn('"$RM" -f "$home_dir/.local/bin/omarchy-dropdown-terminal"', cli)
        self.assertIn('"$LN" -sfn', cli)
        self.assertIn('home_dir="$(validate_home)"', cli)

    def test_cli_need_guards_match_the_python_resolver(self):
        # The bash twin must reject what resolve() rejects: `.`/`..`, directories,
        # and symlinks whose target lives in an untrusted directory.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            cli = handle.read()
        self.assertIn('""|"."|"..") die "invalid tool name', cli)
        self.assertIn("*..*) die \"invalid tool name", cli)
        self.assertIn('[ -f "$real" ]', cli)          # regular file only
        self.assertIn('dir_trusted "${real%/*}"', cli)  # resolved path's directory
        self.assertIn("READLINK_BIN", cli)

    def test_socket_path_matches_the_unit_that_binds_it(self):
        # The CLI used to build "<runtime>/foot-dropdown-terminal.sock" itself and a
        # refactor dropped the filename: footclient was handed the runtime DIRECTORY
        # and the ownership guard then aborted every first summon. The DIRECTORY must
        # also be systemd's %t, which is what the unit binds - not this process's
        # XDG_RUNTIME_DIR.
        sock = backend.socket_path(TEST_UIDS)
        self.assertTrue(sock.endswith("/foot-dropdown-terminal.sock"), sock)
        self.assertEqual(os.path.dirname(sock), f"/run/user/{os.getuid()}")
        unit_bind = "--server=%t/foot-%i.sock".replace("%i", backend.UNIT_INSTANCE)
        self.assertEqual(os.path.basename(sock), os.path.basename(unit_bind))
        self.assertIn("--server=%t/foot-%i.sock", backend.unit_body("/usr/bin/foot"))
        self.assertEqual(backend.FOOT_SOCKET_NAME, f"foot-{backend.UNIT_INSTANCE}.sock")

    def test_state_path_no_longer_diverges_from_the_watcher(self):
        import focus_watcher

        runtime, state = backend.runtime_paths(TEST_UIDS)
        self.assertEqual(os.path.basename(state), focus_watcher.STATE_NAME)

    def test_bind_uses_the_absolute_cli_path_not_path_lookup(self):
        # Omarchy's o.bind turns a string dispatcher into hl.dsp.exec_cmd, i.e. a
        # shell command resolved through the compositor's PATH.
        self.assertIn(
            f"{backend.HOME}/.local/bin/omarchy-dropdown-terminal toggle", backend.bind_body()
        )
        self.assertNotIn('"omarchy-dropdown-terminal toggle"', backend.bind_body())

    def test_config_dir_follows_xdg_config_home(self):
        # Hyprland (and the Lua hook) find the config through XDG_CONFIG_HOME: writing
        # to $HOME/.config while it points elsewhere installs rules nobody loads, so the
        # write commands require it to be valid instead of silently falling back.
        #
        # The fixture must NOT come from tempfile's default root: $TMPDIR is often /tmp,
        # which is world-writable and therefore correctly refused by the candidate rules -
        # asserting there would test the machine's temp policy, not this behaviour. A dir
        # under the real home has an ancestor chain that qualifies on any normal host.
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        cache = os.path.join(real_home, ".cache")
        os.makedirs(cache, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as tmp:
            with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                self.assertEqual(
                    backend._validated_config_dir(TEST_UIDS), os.path.realpath(tmp)
                )
            with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path"}):
                self.assertEqual(
                    backend._validated_config_dir(TEST_UIDS), f"{backend.HOME}/.config"
                )
            with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/etc"}):
                # not ours: never write there
                self.assertEqual(
                    backend._validated_config_dir(TEST_UIDS), f"{backend.HOME}/.config"
                )
            with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/etc"}):
                with self.assertRaises(SystemExit):
                    backend.require_config_dir(TEST_UIDS)
        # the module constants were derived at import from this sandbox
        self.assertTrue(backend.HYPRLAND_DIR.startswith(SANDBOX))

    def test_unreadable_config_aborts_before_touching_systemd(self):
        # Uninstall used to disable+mask the unit and then raise on the config read,
        # leaving a half-removed plugin that could not be retried.
        with open(backend.BINDINGS_LUA, "w") as handle:
            handle.write("x" * (backend.MAX_CONFIG_BYTES + 10))
        backend.CALLS.clear()
        with self.assertRaises(backend.ConfigUnreadable):
            backend.uninstall()
        self.assertEqual(backend.CALLS, [], "systemctl ran before the config check")

    def test_backup_replaces_a_planted_symlink_instead_of_following_it(self):
        os.makedirs(backend.SYSTEMD_USER_DIR, exist_ok=True)
        target = os.path.join(SANDBOX, "elsewhere")
        with open(backend.UNIT_DST_PATH, "w") as handle:
            handle.write("# foreign unit\n")
        os.symlink(target, backend.UNIT_DST_PATH + ".pre-dropdown-terminal.bak")
        backend.install_unit("/usr/bin/foot")
        self.assertTrue(os.path.isfile(backend.UNIT_DST_PATH + ".pre-dropdown-terminal.bak"))
        self.assertFalse(os.path.lexists(target), "the symlink target was written through")

    def test_ensure_server_waits_for_the_socket_on_the_cold_path(self):
        # Two review rounds missed this: the socket wait must run on the COLD path
        # (unit not active at entry), which is exactly where the first client is
        # spawned. This drives the real function body with stubs - if the early
        # `return 0` ever comes back, the cold case returns 0 without waiting and this
        # test fails.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            source = handle.read()
        start = source.index("ensure_server() {")
        end = source.index("\n}\n", start) + 3
        func = source[start:end]
        script = (
            "set -uo pipefail\n"
            "UNIT=foot-server@test.service\n"
            "SLEEP=/bin/true\n"
            "STATE=$(mktemp)\n"
            "DIR=$(mktemp -d)\n"
            'SOCK="$DIR/s.sock"\n'
            'SHIM="$DIR/systemctl"\n'
            'printf \'#!/bin/sh\\nprintf active > "%s"\\n\' "$STATE" > "$SHIM"\n'
            'chmod +x "$SHIM"\n'
            'SYSTEMCTL="$SHIM"\n'
            'server_active() { [ "$(cat "$STATE")" = active ]; }\n'
            'server_socket() { printf %s "$SOCK"; }\n'
            'die() { printf "die: %s\\n" "$*" >&2; exit 7; }\n'
            'sleep() { :; }\n'
            + func + "\n"
            # cold path: inactive at entry, and the socket NEVER appears -> must die
            'printf inactive > "$STATE"\n'
            'ensure_server || printf "cold_rc=%s\\n" "$?"\n'
        )
        cold = terminal_run_bash(script)
        # The cold path must reach the socket wait and fail there: before the fix it
        # returned 0 from the is-active loop and printed nothing, so the presence of
        # this message (and the absence of a success line) is the proof.
        self.assertIn("socket did not appear", cold, "the cold path skipped the wait")
        self.assertNotIn("cold_rc=0", cold)

        # warm path: socket present -> success, no die
        script_warm = script.replace(
            'printf inactive > "$STATE"', 'printf active > "$STATE"\nprintf x > /dev/null'
        ).replace(
            'ensure_server || printf "cold_rc=%s\\n" "$?"',
            'python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); '
            's.bind(sys.argv[1])" "$SOCK" 2>/dev/null || touch "$SOCK"\n'
            'ensure_server; printf "warm_rc=%s\\n" "$?"',
        )
        warm = terminal_run_bash(script_warm)
        self.assertIn("warm_rc=0", warm)

    def test_cli_and_watcher_match_the_same_workspace_exactly(self):
        # A suffix match (name.endswith("dropdown")) made the CLI report a foreign
        # workspace (special:xdropdown) as shown while the watcher said hidden.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            cli = handle.read()
        self.assertIn('name == "special:dropdown"', cli)
        self.assertIn('ws == "special:dropdown"', cli)
        self.assertNotIn('endswith("dropdown")', cli)
        import focus_watcher

        self.assertEqual(focus_watcher.SPECIAL_WS, "special:dropdown")

    def test_read_text_refuses_a_fifo(self):
        # A FIFO at a config path makes open() block forever; a byte cap cannot help.
        fifo = os.path.join(SANDBOX, "fifo.lua")
        os.mkfifo(fifo)
        try:
            with self.assertRaises(backend.ConfigUnreadable):
                backend.read_text(fifo)
        finally:
            os.unlink(fifo)

    def test_atomic_write_replaces_a_foreign_owned_symlink(self):
        # os.stat (following) made the mode/owner copy chown to the LINK TARGET's
        # owner and raise; the link must simply be replaced.
        target = os.path.join(SANDBOX, "foreign-target")
        with open(target, "w") as handle:
            handle.write("foreign\n")
        link = os.path.join(SANDBOX, "link.lua")
        os.symlink(target, link)
        backend.atomic_write(link, "ours\n")     # must not raise
        self.assertFalse(os.path.islink(link))
        with open(link) as handle:
            self.assertEqual(handle.read(), "ours\n")

    def test_write_commands_refuse_an_unsafe_xdg_config_home(self):
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/ddt-unsafe"}):
            with self.assertRaises(SystemExit) as caught:
                backend.require_config_dir()
        self.assertIn("XDG_CONFIG_HOME", str(caught.exception))

    def test_uninstall_unmasks_even_when_a_block_is_corrupt(self):
        # A half-removed plugin (unit masked, keybind left) could not be retried.
        os.makedirs(os.path.dirname(backend.BINDINGS_LUA), exist_ok=True)
        with open(backend.BINDINGS_LUA, "w") as handle:
            handle.write(backend.BIND_BEGIN + "\n")     # no END line
        backend.CALLS.clear()
        backend.uninstall()
        verbs = [argv[1:3] for argv in backend.CALLS if argv[0].endswith("systemctl")]
        self.assertIn(["--user", "mask"], verbs)
        self.assertIn(["--user", "unmask"], verbs)

    def test_uninstall_prefights_config_dir_before_masking(self):
        # An unusable XDG_CONFIG_HOME must abort BEFORE systemd is touched: no mask
        # without unmask, no half-removed plugin.
        backend.CALLS.clear()
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/etc"}):
            with self.assertRaises(SystemExit):
                backend.uninstall()
        verbs = [argv[1:3] for argv in backend.CALLS if argv[0].endswith("systemctl")]
        self.assertEqual(verbs, [], "systemd ran before the config preflight")

    def test_uninstall_unmasks_when_removal_fails_after_mask(self):
        # A failure inside step 2 (after mask) must still unmask: simulate a lock
        # failure and assert unmask + reload still run.
        real_remove = backend.remove_block
        backend.remove_block = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("cannot open the lock file")
        )
        try:
            backend.CALLS.clear()
            backend.uninstall()
        finally:
            backend.remove_block = real_remove
        verbs = [argv[1:3] for argv in backend.CALLS if argv[0].endswith("systemctl")]
        self.assertIn(["--user", "mask"], verbs)
        self.assertIn(["--user", "unmask"], verbs)

    def test_uninstall_leaves_a_foreign_rules_file_alone(self):
        # A rules file without our marker is someone else's config: uninstall
        # must not delete it.
        os.makedirs(os.path.dirname(backend.RULES_PATH), exist_ok=True)
        with open(backend.RULES_PATH, "w") as handle:
            handle.write("-- someone else's config\n")
        backend.uninstall()
        with open(backend.RULES_PATH) as handle:
            self.assertEqual(handle.read(), "-- someone else's config\n")

    def test_uninstall_restores_backed_up_rules_file(self):
        os.makedirs(os.path.dirname(backend.RULES_PATH), exist_ok=True)
        with open(backend.RULES_PATH, "w") as handle:
            handle.write(backend.RULES_BEGIN + "\n" + "rules\n" + backend.RULES_END + "\n")
        with open(backend.RULES_PATH + ".pre-dropdown-terminal.bak", "w") as handle:
            handle.write("-- original rules\n")
        backend.uninstall()
        with open(backend.RULES_PATH) as handle:
            self.assertEqual(handle.read(), "-- original rules\n")

    def test_keybind_path_with_space_is_quoted_not_refused(self):
        # A HOME with a space or UTF-8 name is legitimate: the path is shell-quoted
        # (then Lua-escaped), not refused, and install writes nothing half-done.
        real_home = backend.HOME
        try:
            backend.HOME = os.path.join(SANDBOX, "my home josé")
            os.makedirs(os.path.join(backend.HOME, ".config", "hypr"), exist_ok=True)
            body = backend.bind_body()
            self.assertIn("omarchy-dropdown-terminal", body)
            self.assertIn("toggle", body)
            # shell-parseable: shlex must recover the path + toggle
            import shlex

            dispatcher = body.split('"')[5]
            parts = shlex.split(dispatcher)
            self.assertEqual(parts[-1], "toggle")
            self.assertTrue(parts[0].endswith("omarchy-dropdown-terminal"))
        finally:
            backend.HOME = real_home

    def test_symlinked_hypr_dir_outside_home_is_accepted_when_safe(self):
        # ~/.config/hypr -> /mnt/dotfiles/hypr is a normal dotfiles layout: refuse
        # only when the target is unsafe, not merely because it is outside HOME.
        # (The target lives inside the sandbox so its ancestors are safe; a target
        # under a world-writable ancestor such as /tmp is still refused.)
        outside = os.path.join(SANDBOX, "dotfiles")
        os.makedirs(outside, exist_ok=True)
        os.chmod(outside, 0o700)
        link_parent = os.path.join(SANDBOX, ".config")
        os.makedirs(link_parent, exist_ok=True)
        link = os.path.join(link_parent, "hypr-linktest")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(outside, link)
        try:
            backend._require_safe_write_target(os.path.join(link, "dropdown-terminal.lua"))
        finally:
            os.unlink(link)

    def test_dangling_symlink_reads_as_absent(self):
        link = os.path.join(SANDBOX, "dangling.lua")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.join(SANDBOX, "not-here.lua"), link)
        self.assertIsNone(backend.read_text(link))

    def test_every_panel_process_is_started_and_panel_uses_no_raw_env_path(self):
        # A Process whose running flag is never set is dead code (that is how the
        # state-path query shipped inert). Each declared Process id must either set
        # `running: true` inline or be started by an explicit assignment.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "Panel.qml")) as handle:
            panel = handle.read()
        blocks = panel.split("Process {")[1:]
        self.assertTrue(blocks, "expected Process blocks in Panel.qml")
        for block in blocks:
            match = re.search(r"id:\s*(\w+)", block)
            name = match.group(1) if match else ""
            self.assertTrue(name, f"Process block without an id: {block[:80]}")
            started = re.search(r"running:\s*true", block) or re.search(
                rf"{name}\.running\s*=\s*true", panel
            )
            self.assertTrue(started, f"Panel.qml Process '{name}' is never started")

    def test_cli_never_invokes_a_tool_by_bare_name(self):
        # The CLI runs with no PATH at all (it is started with a cleared
        # environment), so every tool must go through need()/$VARS. The regex must
        # catch indented command positions too - a bare `rm` slipped through a
        # line-start-only pattern once already.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        path = os.path.join(repo, "bin", "omarchy-dropdown-terminal")
        with open(path) as handle:
            lines = handle.readlines()
        source = "".join(lines)
        self.assertNotIn("command -v", source)
        self.assertNotIn("/usr/bin/env", source)
        bare = re.compile(
            r"(?:^|[;&|(`])\s*(python3|hyprctl|systemctl|sleep|setsid|footclient"
            r"|mkdir|ln|rm|stat|readlink|id|dirname)\s"
        )
        for number, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue
            self.assertIsNone(
                bare.search(line), f"bare tool invocation on line {number}: {line.strip()}"
            )
        # and the resolver is the single place that decides trust
        self.assertIn("need() {", source)
        self.assertIn("dir_trusted() {", source)
        # every backend invocation goes through the isolated interpreter
        bare_backend = re.compile(r'"\$PYTHON"\s+"\$BACKEND"')
        self.assertIsNone(bare_backend.search(source), "backend run without -I -E -S")
        self.assertIn('"$PYTHON" -I -E -S "$BACKEND"', source)
        # ... and so does every inline python probe
        bare_probe = re.compile(r'"\$PYTHON"\s+-c')
        self.assertIsNone(bare_probe.search(source), "inline probe without -I -E -S")
        # the socket type test must not parse a TRANSLATED stat string (comments
        # explain the trap and are allowed to name it)
        code = "\n".join(line for line in lines if not line.strip().startswith("#"))
        self.assertNotIn("%F", code)
        self.assertIn("16#f000", code)
        # the real uid is never taken from the environment
        self.assertNotIn('REAL_UID="${UID', code)

    def test_snapshot_contracts(self):
        # The merged snapshot() replaces dropdown_probe + client_count +
        # window_in_special: one query, one parser, same failure contracts.
        # Driven with stubbed HYPRCTL outputs so no compositor is needed.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            source = handle.read()
        for name in ("snapshot() {", "parse_snapshot() {"):
            self.assertIn(name, source)
        start = source.index("snapshot() {")
        end = source.index("\n}\n", start) + 3
        snap_func = source[start:end]
        start = source.index("parse_snapshot() {")
        end = source.index("\n}\n", start) + 3
        parse_func = source[start:end]
        lib = snap_func + "\n" + parse_func + "\n"
        cases = [
            # (monitors payload, clients payload, visible, count, inspecial)
            ('[{"specialWorkspace":{"name":"special:dropdown"}}]', "[]", 0, 0, 0),
            ('[{"specialWorkspace":{"name":""}}]', "[]", 1, 0, 0),
            ("[]", "[]", 1, 0, 0),
            ("", "", 3, 0, 0),  # hyprctl died: refuse, never "hidden"
            ("not json{{{", "[]", 3, 0, 0),
            ("[]", "not json{{{", 1, 0, 0),  # bad clients: count fallback
            ('[{"specialWorkspace":{"name":""}}]',
             '[{"class":"org.omarchy.dropdown-terminal","workspace":{"name":"3"}}]',
             1, 1, 0),  # stranded window
            ('[{"specialWorkspace":{"name":""}}]',
             '[{"class":"org.omarchy.dropdown-terminal",'
             '"workspace":{"name":"special:dropdown"}}]',
             1, 1, 1),  # placed window
            ('[{"specialWorkspace":{"name":"special:xdropdown"}}]', "[]",
             1, 0, 0),  # exact match only, no suffix
            ('[{"specialWorkspace":5}]', "[]", 3, 0, 0),  # nested shape: refuse
        ]
        # Oversized payloads, past the parser caps (MON_MAX 262144,
        # CLI_MAX 4194304): built here (payloads that big cannot be literals).
        big_shown_mons = (
            '[{"specialWorkspace":{"name":"special:dropdown"}},'
            + '{"specialWorkspace":{"name":"%s","id":%d}},' * 2000
            % tuple(v for i in range(2000) for v in ("x" * 100, i))
            + '{"specialWorkspace":{"name":""}}]'
        )
        assert len(big_shown_mons) > 262144, len(big_shown_mons)
        big_cli = (
            '[{"class":"org.omarchy.dropdown-terminal",'
            '"workspace":{"name":"special:dropdown"},"pad":"%s"}]' % ("y" * 300000)
        )
        assert 262144 < len(big_cli) <= 4194304, len(big_cli)
        cases += [
            # Oversized but otherwise VALID monitors: old probe refused (exit 3),
            # so the merged parser must refuse too, not act on what it saw.
            (big_shown_mons, "[]", 3, 0, 0),
            # Oversized clients: old client_count fell back to 0; INSPECIAL
            # keeps the old window_in_special cap (MON_MAX), so it stays 0 too.
            ("[]", big_cli, 1, 1, 0),
            # Non-dict client entries: skipped, never crash, never counted.
            ("[]", '["x", 5, null]', 1, 0, 0),
        ]
        # Harness: stub HYPRCTL as a shell FUNCTION (functions see the
        # test shell's STUBDIR; a script file would not, since snapshot()
        # runs it via "$HYPRCTL" with only the exported env).
        harness = (
            "set -uo pipefail\n"
            + lib +
            "STUBDIR=$(mktemp -d)\n"
            "export STUBDIR\n"
            "set +u\n"
            'cp "$MONS_FILE" "$STUBDIR/mons.json"\n'
            'cp "$CLS_FILE" "$STUBDIR/cls.json"\n'
            "hyprctl() { if [ \"$1\" = monitors ]; then cat \"$STUBDIR/mons.json\"; "
            "else cat \"$STUBDIR/cls.json\"; fi; }\n"
            "HYPRCTL=hyprctl\n"
            "PYTHON=/usr/bin/python3\n"
            's="$(snapshot)"\n'
            'parse_snapshot "$s"\n'
            'printf "V=%s C=%s I=%s\\n" "$SNAP_VISIBLE" "$SNAP_COUNT" "$SNAP_INSPECIAL"\n'
        )
        for mons, clients, visible, count, inspecial in cases:
            # Payloads past the parser caps ride in files (argv+env have a
            # kernel size limit); the stub reads them by path when set.
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".mons", delete=False
            ) as mons_file:
                mons_file.write(mons)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".clients", delete=False
            ) as clients_file:
                clients_file.write(clients)
            try:
                env = dict(
                    os.environ,
                    MONS_FILE=mons_file.name,
                    CLS_FILE=clients_file.name,
                )
                completed = subprocess.run(
                    ["/usr/bin/bash", "-c", harness],
                    capture_output=True, text=True, timeout=60, env=env,
                )
            finally:
                os.unlink(mons_file.name)
                os.unlink(clients_file.name)
            self.assertEqual(
                completed.stdout.strip(), f"V={visible} C={count} I={inspecial}",
                f"mons_len={len(mons)} clients_len={len(clients)}: "
                f"{completed.stdout!r}{completed.stderr!r}",
            )

    def test_dir_trust_cache_returns_the_same_verdicts(self):
        # The dir_trusted memoization must not change any verdict: run the same
        # directories twice (cache cold, then warm) plus an untrusted one.
        script = (
            "set -uo pipefail\n"
            'STAT_BIN=/usr/bin/stat\n'
            "source <(sed -n '/^declare -A _DIR_TRUST_CACHE/,/^}/p' "
            + os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "bin", "omarchy-dropdown-terminal")
            + ")\n"
            "dir_trusted /usr/bin; echo \"cold_usrbin=$?\"\n"
            "dir_trusted /usr/bin; echo \"warm_usrbin=$?\"\n"
            "dir_trusted /tmp; echo \"tmp=$?\"\n"
            "dir_trusted /tmp; echo \"tmp2=$?\"\n"
        )
        out = terminal_run_bash(script)
        self.assertIn("cold_usrbin=0", out)
        self.assertIn("warm_usrbin=0", out)
        self.assertIn("tmp=1", out)
        self.assertIn("tmp2=1", out)


if __name__ == "__main__":
    unittest.main()
