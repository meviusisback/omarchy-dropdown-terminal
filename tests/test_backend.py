#!/usr/bin/env python3
"""Tests for the dropdown-terminal backend: idempotency, atomicity, uninstall.

Runs against a sandboxed HOME (never touches the real user config):
HOME is repointed before the backend module is imported.
"""

import importlib
import os
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

    def test_bind_block_roundtrip(self):
        backend.atomic_write(backend.BINDINGS_LUA, "-- my binds\n")
        body = backend.BIND_LINE.split("\n", 1)[1].rsplit("\n", 1)[0]
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


if __name__ == "__main__":
    unittest.main()
