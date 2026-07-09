#!/usr/bin/env python3
"""Unit tests for auto_review_installer.py."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


INSTALLER_PATH = Path(__file__).with_name("auto_review_installer.py")
SPEC = importlib.util.spec_from_file_location("auto_review_installer", INSTALLER_PATH)
assert SPEC is not None
assert SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class CodexCommandResolutionTest(unittest.TestCase):
    def test_windows_prefers_cmd_shim(self) -> None:
        def fake_which(command: str) -> str | None:
            return {
                "codex.cmd": r"C:\Tools\nodejs\codex.cmd",
                "codex.exe": r"C:\Program Files\Codex\codex.exe",
                "codex": r"C:\Users\User\AppData\Roaming\npm\codex",
            }.get(command)

        with (
            mock.patch.object(installer.os, "name", "nt"),
            mock.patch.object(installer.shutil, "which", side_effect=fake_which) as which,
        ):
            self.assertEqual(installer.find_codex_command(), r"C:\Tools\nodejs\codex.cmd")

        which.assert_called_once_with("codex.cmd")

    def test_windows_falls_back_to_exe_before_bare_name(self) -> None:
        calls: list[str] = []

        def fake_which(command: str) -> str | None:
            calls.append(command)
            return {
                "codex.exe": r"C:\Program Files\Codex\codex.exe",
                "codex": r"C:\Users\User\AppData\Roaming\npm\codex",
            }.get(command)

        with (
            mock.patch.object(installer.os, "name", "nt"),
            mock.patch.object(installer.shutil, "which", side_effect=fake_which),
        ):
            self.assertEqual(installer.find_codex_command(), r"C:\Program Files\Codex\codex.exe")

        self.assertEqual(calls, ["codex.cmd", "codex.exe"])

    def test_non_windows_uses_bare_codex(self) -> None:
        with (
            mock.patch.object(installer.os, "name", "posix"),
            mock.patch.object(installer.shutil, "which", return_value="/usr/local/bin/codex") as which,
        ):
            self.assertEqual(installer.find_codex_command(), "/usr/local/bin/codex")

        which.assert_called_once_with("codex")

    def test_require_codex_runs_version_with_resolved_command(self) -> None:
        codex_home = Path("/tmp/codex-home")
        with (
            mock.patch.object(installer, "find_codex_command", return_value=r"C:\Tools\nodejs\codex.cmd"),
            mock.patch.object(installer, "run_command") as run_command,
        ):
            self.assertEqual(installer.require_codex(codex_home), r"C:\Tools\nodejs\codex.cmd")

        run_command.assert_called_once_with(
            [r"C:\Tools\nodejs\codex.cmd", "--version"],
            codex_home,
        )

    def test_require_codex_fails_when_missing(self) -> None:
        with mock.patch.object(installer, "find_codex_command", return_value=None):
            with self.assertRaises(SystemExit) as raised:
                installer.require_codex(Path("/tmp/codex-home"))

        self.assertEqual(
            str(raised.exception),
            "Error: Codex CLI is required, but 'codex' was not found in PATH.",
        )


if __name__ == "__main__":
    unittest.main()
