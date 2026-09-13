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
import re
import sys
import tempfile
import unittest
import unittest.mock

SANDBOX = tempfile.mkdtemp(prefix="ddt-test-")
os.environ["HOME"] = SANDBOX
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

# Uids the ancestor walk may trust in this environment: the dev sandbox maps uid 0
# to 65534 (unmapped in the user namespace), so tests inject that too.
TEST_UIDS = (0, os.getuid(), os.stat("/").st_uid)

backend = importlib.import_module("dropdown_terminal")


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
        body = backend.BIND_BODY
        self.assertTrue(backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, body, backend.BIND_END))
        content = backend.read_text(backend.BINDINGS_LUA)
        self.assertIn('o.bind("SUPER + U"', content)
        self.assertIn("-- my binds", content)

        # Idempotent: second append is a no-op, no duplication.
        self.assertFalse(backend.append_block(backend.BINDINGS_LUA, backend.BIND_BEGIN, body, backend.BIND_END))
        self.assertEqual(backend.read_text(backend.BINDINGS_LUA).count("o.bind("), 1)

    def test_remove_block(self):
        backend.atomic_write(backend.BINDINGS_LUA, "before\n" + backend.BIND_LINE + "after\n")
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
        backend.atomic_write(backend.BINDINGS_LUA, backend.BIND_LINE)
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
        self.assertIn('"$PYTHON" -I -E -S "$BACKEND" socket-path', cli)
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
        # and the new ownership guard then aborted every first summon. Assert the
        # value (behaviour) and that it still matches the unit's %t/foot-%i.sock.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        sock = backend.socket_path(TEST_UIDS)
        self.assertTrue(sock.endswith("/foot-dropdown-terminal.sock"), sock)
        self.assertEqual(os.path.dirname(sock), backend.runtime_paths(TEST_UIDS)[0])
        unit_bind = "--server=%t/foot-%i.sock".replace("%i", backend.UNIT_INSTANCE)
        self.assertTrue(sock.endswith("/" + os.path.basename(unit_bind)))
        self.assertIn("--server=%t/foot-%i.sock", backend.unit_body("/usr/bin/foot"))
        with open(os.path.join(repo, "bin", "omarchy-dropdown-terminal")) as handle:
            cli = handle.read()
        self.assertIn('"$PYTHON" -I -E -S "$BACKEND" socket-path', cli)
        self.assertIn("run_backend state-path", cli)
        # the CLI must ask for the socket FILE, not the directory
        self.assertNotIn('"$BACKEND" runtime-dir', cli)

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


if __name__ == "__main__":
    unittest.main()
