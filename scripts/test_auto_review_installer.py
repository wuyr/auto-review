#!/usr/bin/env python3
"""Unit tests for auto_review_installer.py."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


INSTALLER_PATH = Path(__file__).with_name("auto_review_installer.py")
SPEC = importlib.util.spec_from_file_location("auto_review_installer", INSTALLER_PATH)
assert SPEC is not None
assert SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def hooks_payload() -> dict:
    command = 'python3 -X utf8 -c "auto_review_hook.py"'
    return {
        "hooks": {
            event_name: [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
            for event_name in installer.HOOK_EVENTS
        }
    }


def write_hooks(plugin_root: Path) -> None:
    hooks = plugin_root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps(hooks_payload()),
        encoding="utf-8",
    )
    (hooks / "auto_review_hook.py").write_text("pass\n", encoding="utf-8")


def supports_directory_symlinks() -> bool:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "source"
        target = root / "target"
        source.mkdir()
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            return False
        return target.is_symlink()


SYMLINKS_SUPPORTED = supports_directory_symlinks()


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


class HookPythonConfigurationTest(unittest.TestCase):
    def test_windows_hook_uses_quoted_installer_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = Path(temp_dir) / "plugin"
            write_hooks(plugin_root)

            executable = r"C:\Program Files\Python312\python.exe"
            installer.configure_hook_python(plugin_root, executable, platform_name="nt")

            payload = json.loads(
                (plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8")
            )
            for event_name in installer.HOOK_EVENTS:
                command = payload["hooks"][event_name][0]["hooks"][0]["command"]
                self.assertTrue(command.startswith(f'"{executable}" -X utf8 -c '), command)
                self.assertNotIn("python3 -X utf8 -c", command)

    def test_posix_hook_shell_quotes_installer_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = Path(temp_dir) / "plugin"
            write_hooks(plugin_root)

            installer.configure_hook_python(
                plugin_root,
                "/opt/Python Runtime/bin/python3",
                platform_name="posix",
            )

            payload = json.loads(
                (plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8")
            )
            command = payload["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertTrue(
                command.startswith("'/opt/Python Runtime/bin/python3' -X utf8 -c "),
                command,
            )


class MarketplaceCompatibilityTest(unittest.TestCase):
    def test_existing_marketplace_name_drives_plugin_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_marketplace = root / "source-marketplace.json"
            source_marketplace.write_text(
                json.dumps({"name": "review-runtime-local", "plugins": []}),
                encoding="utf-8",
            )
            target_root = root / "codex-home"
            target_marketplace = target_root / ".agents" / "plugins" / "marketplace.json"
            target_marketplace.parent.mkdir(parents=True)
            target_marketplace.write_text(
                json.dumps(
                    {
                        "name": "local",
                        "interface": {"displayName": "Local Plugins"},
                        "plugins": [{"name": "carry-on"}],
                    }
                ),
                encoding="utf-8",
            )

            market_name = installer.write_or_link_marketplace(
                {"marketplace": source_marketplace},
                target_root,
                "copy",
            )

            self.assertEqual(market_name, "local")
            payload = json.loads(target_marketplace.read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["name"] for entry in payload["plugins"]],
                ["carry-on", installer.PLUGIN_ID],
            )

    def test_shared_marketplace_config_is_preserved_on_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            config = codex_home / "config.toml"
            config.write_text(
                "[marketplaces.local]\nsource = 'local'\n\n"
                '[plugins."review-runtime@local"]\nenabled = true\n\n'
                '[hooks.state."review-runtime@local:hooks/hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:test"\n\n'
                '[plugins."carry-on@local"]\nenabled = true\n',
                encoding="utf-8",
            )

            installer.remove_config_sections(
                codex_home,
                installer.PLUGIN_ID,
                "local",
                remove_marketplace=False,
            )

            updated = config.read_text(encoding="utf-8")
            self.assertIn("[marketplaces.local]", updated)
            self.assertIn('[plugins."carry-on@local"]', updated)
            self.assertNotIn("review-runtime@local", updated)

    def test_uninstall_keeps_shared_target_marketplace_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "source"
            source_marketplace = project_root / ".agents" / "plugins" / "marketplace.json"
            source_marketplace.parent.mkdir(parents=True)
            source_marketplace.write_text(
                json.dumps({"name": "review-runtime-local", "plugins": []}),
                encoding="utf-8",
            )

            codex_home = root / "codex-home"
            target_marketplace = codex_home / ".agents" / "plugins" / "marketplace.json"
            target_marketplace.parent.mkdir(parents=True)
            target_marketplace.write_text(
                json.dumps(
                    {
                        "name": "local",
                        "plugins": [
                            {"name": "carry-on"},
                            {"name": installer.PLUGIN_ID},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = installer.argparse.Namespace(
                project_root=str(project_root),
                target_root=str(codex_home),
                codex_home=str(codex_home),
                keep_files=True,
            )

            with (
                mock.patch.object(installer, "find_codex_command", return_value="codex"),
                mock.patch.object(installer, "run_command") as run_command,
                mock.patch("builtins.print"),
            ):
                installer.uninstall(args)

            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertIn(
                ["codex", "plugin", "remove", f"{installer.PLUGIN_ID}@local"],
                commands,
            )
            self.assertNotIn(
                ["codex", "plugin", "marketplace", "remove", "local"],
                commands,
            )
            payload = json.loads(target_marketplace.read_text(encoding="utf-8"))
            self.assertEqual([entry["name"] for entry in payload["plugins"]], ["carry-on"])


class AppServerResponseTest(unittest.TestCase):
    def test_response_queue_works_for_pipe_reader_output(self) -> None:
        responses = installer.queue.Queue()
        responses.put('{"method":"notification"}\n')
        responses.put('{"id":7,"result":{"status":"ok"}}\n')

        self.assertEqual(
            installer.app_server_request(responses, 7, timeout_seconds=0.1),
            {"status": "ok"},
        )


class InstallPathSafetyTest(unittest.TestCase):
    @unittest.skipUnless(SYMLINKS_SUPPORTED, "directory symlinks are unavailable")
    def test_force_replaces_existing_source_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_source = root / "source" / "plugins" / installer.PLUGIN_DIR_NAME
            skill_source = root / "source" / "skills" / installer.SKILL_NAME
            write_hooks(plugin_source)
            skill_source.mkdir(parents=True)

            codex_home = root / "codex-home"
            plugin_dest = codex_home / "plugins" / installer.PLUGIN_DIR_NAME
            skill_dest = codex_home / "skills" / installer.SKILL_NAME
            plugin_dest.parent.mkdir(parents=True)
            skill_dest.parent.mkdir(parents=True)
            plugin_dest.symlink_to(plugin_source, target_is_directory=True)
            skill_dest.symlink_to(skill_source, target_is_directory=True)

            installer.install_plugin_and_skill(
                {"plugin": plugin_source, "skill": skill_source},
                codex_home,
                codex_home,
                "symlink",
                True,
                r"C:\Python312\python.exe",
            )

            self.assertFalse(plugin_dest.is_symlink())
            self.assertTrue(skill_dest.is_symlink())
            self.assertEqual(skill_dest.resolve(), skill_source.resolve())
            self.assertFalse((plugin_dest / "hooks" / "hooks.json").is_symlink())
            self.assertTrue((plugin_dest / "hooks" / "auto_review_hook.py").is_symlink())
            configured = json.loads(
                (plugin_dest / "hooks" / "hooks.json").read_text(encoding="utf-8")
            )
            command = configured["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertNotIn("python3 -X utf8 -c", command)


if __name__ == "__main__":
    unittest.main()
