#!/usr/bin/python3
"""Tests for backend/proc.py: trusted-path resolution and bounded execution.

The resolve() tests run the production logic unprivileged by passing their own
candidate directory, owner uid and trust root: the dev/test environment cannot
create root-owned files (and inside the Orca bubblewrap sandbox root-owned paths
read as `nobody`), so exercising the same code with fixtures we own is the only
honest way to cover both the accept and the reject paths here. The production
defaults (root-owned, the fixed /usr/bin-style directories) are asserted too.
"""

import os
import sys
import tempfile
import time
import unittest

BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

import proc  # noqa: E402

PY = os.path.realpath(sys.executable)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        # The cache is per-process and keyed on (name, dirs, uid, trust_root);
        # clear it so each case judges the fixture's current mode.
        proc._resolved.clear()
        self.dir = tempfile.mkdtemp(prefix="ddt-proc-")
        os.chmod(self.dir, 0o700)
        self.uid = os.getuid()

    def tearDown(self):
        proc._resolved.clear()

    def _tool(self, name, mode=0o755):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\ntrue\n")
        os.chmod(path, mode)
        return path

    def _resolve(self, name, uid=None):
        return proc.resolve(
            name, dirs=(self.dir,), uid=self.uid if uid is None else uid,
            trust_root=self.dir,
        )

    def test_accepts_owned_executable(self):
        created = self._tool("good")
        self.assertEqual(self._resolve("good"), created)

    def test_resolves_symlink_to_its_target(self):
        target = self._tool("real")
        os.symlink(target, os.path.join(self.dir, "linked"))
        self.assertEqual(self._resolve("linked"), target)

    def test_rejects_world_writable_directory(self):
        self._tool("good")
        os.chmod(self.dir, 0o777)
        self.assertIsNone(self._resolve("good"))

    def test_rejects_group_writable_directory(self):
        self._tool("good")
        os.chmod(self.dir, 0o770)
        self.assertIsNone(self._resolve("good"))

    def test_rejects_world_writable_tool(self):
        self._tool("good", mode=0o777)
        self.assertIsNone(self._resolve("good"))

    def test_rejects_non_executable_tool(self):
        self._tool("good", mode=0o644)
        self.assertIsNone(self._resolve("good"))

    def test_rejects_foreign_owner(self):
        self._tool("good")
        self.assertIsNone(self._resolve("good", uid=self.uid + 1))

    def test_rejects_directory_instead_of_file(self):
        os.mkdir(os.path.join(self.dir, "adir"), 0o755)
        self.assertIsNone(self._resolve("adir"))

    def test_rejects_names_outside_the_charset(self):
        for name in ("../../etc/passwd", "..", "bad name", "", "a/b", "x;y"):
            self.assertIsNone(self._resolve(name), name)

    def test_never_consults_path(self):
        self._tool("pathonly")
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.dir + os.pathsep + original
        try:
            self.assertIsNone(
                proc.resolve("pathonly", dirs=("/nonexistent",), uid=self.uid,
                             trust_root=self.dir)
            )
        finally:
            os.environ["PATH"] = original

    def test_tool_raises_when_unresolvable(self):
        self.assertRaises(proc.ToolNotFound, proc.tool, "definitely-not-a-tool")

    def test_production_defaults_are_root_owned_system_dirs(self):
        self.assertEqual(proc.TRUSTED_UID, 0)
        self.assertEqual(proc.CANDIDATE_DIRS, ("/usr/bin", "/bin", "/usr/local/bin"))


class RunTest(unittest.TestCase):
    def test_rejects_relative_argv0(self):
        self.assertRaises(ValueError, proc.run, ["echo", "hi"])

    def test_rejects_non_executable_argv0(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("not code\n")
            path = handle.name
        os.chmod(path, 0o644)
        try:
            self.assertRaises(ValueError, proc.run, [path])
        finally:
            os.unlink(path)

    def test_captures_stdout_and_reports_returncode(self):
        result = proc.run([PY, "-c", "print('hello'); raise SystemExit(3)"], timeout=10)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.truncated)
        self.assertFalse(result.timed_out)

    def test_bounds_output_and_kills_the_writer(self):
        result = proc.run(
            [PY, "-c", "print('x' * 200000)"], timeout=10, limit=4096
        )
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 4096)
        self.assertNotEqual(result.returncode, 0)  # killed, not completed

    def test_timeout_kills_the_group_and_reaps(self):
        start = time.monotonic()
        result = proc.run(
            [PY, "-c", "import time; time.sleep(30)"], timeout=1
        )
        elapsed = time.monotonic() - start
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 10)
        self.assertNotEqual(result.returncode, 0)

    def test_child_gets_a_minimal_environment(self):
        os.environ["DDT_SECRET_MARKER"] = "leak-me"
        try:
            result = proc.run(
                [PY, "-c",
                 "import os; print(sorted(os.environ)); print(os.environ.get('DDT_SECRET_MARKER'))"],
                timeout=10,
            )
        finally:
            os.environ.pop("DDT_SECRET_MARKER", None)
        self.assertNotIn("leak-me", result.stdout)
        self.assertNotIn("DDT_SECRET_MARKER", result.stdout)

    def test_child_path_is_the_controlled_value(self):
        result = proc.run(
            [PY, "-c", "import os; print(os.environ.get('PATH'))"], timeout=10
        )
        self.assertEqual(result.stdout.strip(), proc.CONTROLLED_PATH)

    def test_env_allowlist_keeps_what_helpers_need(self):
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = "test_signature_123"
        try:
            env = proc.build_env()
        finally:
            os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], "test_signature_123")
        self.assertIn("XDG_RUNTIME_DIR", env)
        self.assertIn("HOME", env)
        self.assertNotIn("PWD", env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
