#!/usr/bin/env python3
"""Tests for the dropdown-terminal backend: idempotency, atomicity, uninstall.

Runs against a sandboxed HOME (never touches the real user config):
HOME is repointed before the backend module is imported.
"""

import importlib
import os
import re
import sys
import tempfile
import unittest

SANDBOX = tempfile.mkdtemp(prefix="ddt-test-")
os.environ["HOME"] = SANDBOX
os.environ["DDT_TEST"] = "1"
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

backend = importlib.import_module("dropdown_terminal")


class BackendTest(unittest.TestCase):
    def setUp(self):
        for p in (backend.HYPRLAND_LUA, backend.BINDINGS_LUA, backend.RULES_PATH,
                  backend.UNIT_DST_PATH, backend.UNIT_DST_PATH + ".pre-dropdown-terminal.bak"):
            if os.path.exists(p):
                os.unlink(p)

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
        # call site may leave a tool to be found through PATH. _call() resolves
        # names through proc.resolve(); stub that here (the dev sandbox cannot
        # see root-owned files) and assert every recorded argv[0] is absolute and
        # comes from a trusted candidate directory.
        original = backend.proc.resolve
        backend.proc.resolve = lambda name, *a, **k: f"/usr/bin/{name}"
        backend.CALLS.clear()
        try:
            backend.install(quiet=True)
            backend.uninstall()
        finally:
            backend.proc.resolve = original
        self.assertTrue(backend.CALLS, "expected the install path to call systemctl")
        for argv in backend.CALLS:
            self.assertTrue(os.path.isabs(argv[0]), argv)
            self.assertTrue(
                any(argv[0].startswith(directory + os.sep)
                    for directory in backend.proc.CANDIDATE_DIRS),
                f"{argv[0]} does not come from a trusted candidate directory",
            )

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
        original = backend.proc.resolve
        ddt = os.environ.pop("DDT_TEST", None)
        backend.proc.resolve = lambda *args, **kwargs: None
        try:
            with self.assertRaises(SystemExit) as caught:
                backend.install(quiet=True)
            self.assertIn("missing trusted tools", str(caught.exception))
        finally:
            backend.proc.resolve = original
            if ddt is not None:
                os.environ["DDT_TEST"] = ddt

    def test_plugin_scripts_pin_absolute_interpreters(self):
        # A `#!/usr/bin/env …` shebang is a PATH lookup performed by the kernel,
        # which would reintroduce exactly what the review asked to remove.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        for rel in ("backend/dropdown_terminal.py", "backend/focus_watcher.py",
                    "backend/proc.py", "bin/omarchy-dropdown-terminal", "install.sh"):
            with open(os.path.join(repo, rel)) as handle:
                first = handle.readline().strip()
            self.assertRegex(first, r"^#!/usr/bin/(python3|bash)$", rel)

    def test_panel_launches_the_watcher_without_path_or_inherited_env(self):
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(repo, "Panel.qml")) as handle:
            panel = handle.read()
        self.assertNotIn('["python3"', panel)
        self.assertIn('"/usr/bin/env"', panel)
        self.assertIn('"-i"', panel)
        self.assertIn('"/usr/bin/python3"', panel)
        self.assertIn('"-I"', panel)
        self.assertIn('"-E"', panel)
        self.assertIn('"-S"', panel)

    def test_cli_never_invokes_a_tool_by_bare_name(self):
        # The CLI may run with no PATH at all (the watcher spawns it with a
        # minimal environment), so every tool must go through need()/$VARS.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        path = os.path.join(repo, "bin", "omarchy-dropdown-terminal")
        with open(path) as handle:
            lines = handle.readlines()
        source = "".join(lines)
        self.assertNotIn("command -v", source)
        self.assertNotIn("/usr/bin/env", source)
        bare = re.compile(
            r"(?:^|[;&|(`]\s*)(python3|hyprctl|systemctl|sleep|setsid|footclient"
            r"|mkdir|ln|stat|readlink|id)\s"
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


if __name__ == "__main__":
    unittest.main()
