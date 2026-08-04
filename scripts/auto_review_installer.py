#!/usr/bin/env python3
"""Installer helper for the auto-review Codex plugin and skill."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO


PRODUCT_NAME = "auto-review"
# Keep runtime IDs distinct from the skill name so Codex's shared mention picker has one match.
PLUGIN_ID = "review-runtime"
PLUGIN_DIR_NAME = "auto-review"
SKILL_NAME = "auto-review"
DEFAULT_MARKETPLACE_NAME = "review-runtime-local"
LEGACY_PLUGIN_ID = "auto-review"
LEGACY_MARKETPLACE_NAME = "auto-review-local"
OWNED_PLUGIN_IDS = frozenset((PLUGIN_ID, LEGACY_PLUGIN_ID))
HOOK_EVENTS = ("UserPromptSubmit", "Stop")
HOOK_PYTHON_PREFIX = "python3 -X utf8 -c "


def fail(message: str) -> None:
    raise SystemExit(f"Error: {message}")


def resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"Cannot read {label}: {path}: {exc}")
    if not raw.strip():
        fail(f"{label} is empty: {path}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON in {label}: {path}: {exc}")
    if not isinstance(payload, dict):
        fail(f"{label} must contain a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def command_env(codex_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    return env


def find_codex_command() -> str | None:
    candidates = ["codex.cmd", "codex.exe", "codex"] if os.name == "nt" else ["codex"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path is not None:
            return path
    return None


def run_command(
    args: list[str],
    codex_home: Path,
    *,
    allow_failure: bool = False,
    quiet: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        env=command_env(codex_home),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 and not allow_failure:
        details = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        fail(f"Command failed ({' '.join(args)}): {details or result.returncode}")
    if not quiet:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    return result


def require_codex(codex_home: Path) -> str:
    codex = find_codex_command()
    if codex is None:
        fail("Codex CLI is required, but 'codex' was not found in PATH.")
    run_command([codex, "--version"], codex_home)
    return codex


def source_paths(project_root: Path) -> dict[str, Path]:
    return {
        "project": project_root,
        "plugin": project_root / "plugins" / PLUGIN_DIR_NAME,
        "skill": project_root / "skills" / SKILL_NAME,
        "marketplace": project_root / ".agents" / "plugins" / "marketplace.json",
    }


def check_source_layout(paths: dict[str, Path]) -> None:
    required = (
        paths["plugin"] / ".codex-plugin" / "plugin.json",
        paths["plugin"] / "hooks" / "hooks.json",
        paths["plugin"] / "hooks" / "auto_review_hook.py",
        paths["skill"] / "SKILL.md",
        paths["marketplace"],
    )
    for path in required:
        if not path.is_file():
            fail(f"Required source file is missing: {path}")

    plugin_json = read_json(paths["plugin"] / ".codex-plugin" / "plugin.json", "plugin.json")
    hooks_json = read_json(paths["plugin"] / "hooks" / "hooks.json", "source hooks.json")
    marketplace_json = read_json(paths["marketplace"], "source marketplace.json")

    if "hooks" not in hooks_json or not isinstance(hooks_json["hooks"], dict):
        fail(f"source hooks.json must contain a top-level hooks object: {paths['plugin'] / 'hooks' / 'hooks.json'}")
    unknown_hook_keys = set(hooks_json) - {"hooks"}
    if unknown_hook_keys:
        fail(
            "source hooks.json contains Codex-incompatible top-level fields: "
            + ", ".join(sorted(unknown_hook_keys))
        )
    if not isinstance(marketplace_json.get("plugins"), list):
        fail(f"source marketplace.json field 'plugins' must be an array: {paths['marketplace']}")
    if not isinstance(plugin_json.get("version"), str) or not plugin_json["version"].strip():
        fail("plugin.json must contain a non-empty string version")


def plugin_version(plugin_source: Path) -> str:
    plugin_json = read_json(plugin_source / ".codex-plugin" / "plugin.json", "plugin.json")
    return str(plugin_json["version"]).strip()


def marketplace_name(marketplace_source: Path) -> str:
    payload = read_json(marketplace_source, "source marketplace.json")
    name = payload.get("name")
    return str(name).strip() if isinstance(name, str) and name.strip() else DEFAULT_MARKETPLACE_NAME


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def copy_or_link(source: Path, dest: Path, mode: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "symlink":
        os.symlink(source, dest, target_is_directory=source.is_dir())
    else:
        if source.is_dir():
            shutil.copytree(source, dest)
        else:
            shutil.copy2(source, dest)


def shell_quote_executable(executable: str, platform_name: str | None = None) -> str:
    platform = platform_name or os.name
    if platform == "nt":
        return subprocess.list2cmdline([executable])
    return shlex.quote(executable)


def configure_hook_python(
    plugin_root: Path,
    python_executable: str,
    platform_name: str | None = None,
) -> None:
    hooks_path = plugin_root / "hooks" / "hooks.json"
    payload = read_json(hooks_path, "installed hooks.json")
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        fail(f"installed hooks.json must contain a top-level hooks object: {hooks_path}")

    command_prefix = shell_quote_executable(python_executable, platform_name)
    configured = 0
    for event_name in HOOK_EVENTS:
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            commands = entry.get("hooks")
            if not isinstance(commands, list):
                continue
            for hook in commands:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or "auto_review_hook.py" not in command:
                    continue
                if not command.startswith(HOOK_PYTHON_PREFIX):
                    fail(
                        "auto-review hook command must start with "
                        f"{HOOK_PYTHON_PREFIX!r}: {hooks_path}"
                    )
                hook["command"] = (
                    command_prefix + " -X utf8 -c " + command[len(HOOK_PYTHON_PREFIX) :]
                )
                configured += 1

    if configured < len(HOOK_EVENTS):
        fail(f"Expected to configure at least {len(HOOK_EVENTS)} auto-review hooks in {hooks_path}")
    write_json(hooks_path, payload)


def link_plugin_with_materialized_hooks(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True)
    for child in source.iterdir():
        if child.name == "hooks":
            continue
        os.symlink(child, dest / child.name, target_is_directory=child.is_dir())

    hooks_source = source / "hooks"
    hooks_dest = dest / "hooks"
    hooks_dest.mkdir()
    for child in hooks_source.iterdir():
        target = hooks_dest / child.name
        if child.name == "hooks.json":
            shutil.copy2(child, target)
        else:
            os.symlink(child, target, target_is_directory=child.is_dir())


def install_plugin(source: Path, dest: Path, mode: str) -> None:
    if mode == "symlink":
        link_plugin_with_materialized_hooks(source, dest)
    else:
        shutil.copytree(source, dest)


def install_plugin_and_skill(
    paths: dict[str, Path],
    target_root: Path,
    codex_home: Path,
    mode: str,
    force: bool,
    python_executable: str | None = None,
) -> None:
    plugin_dest = target_root / "plugins" / PLUGIN_DIR_NAME
    skill_dest = codex_home / "skills" / SKILL_NAME

    if not plugin_dest.is_symlink() and resolved(paths["plugin"]) == resolved(plugin_dest):
        fail(f"Plugin install target resolves to the source directory: {plugin_dest}")
    if not skill_dest.is_symlink() and resolved(paths["skill"]) == resolved(skill_dest):
        fail(f"Skill install target resolves to the source directory: {skill_dest}")

    for dest in (plugin_dest, skill_dest):
        if dest.exists() or dest.is_symlink():
            if not force:
                fail(f"Install target exists: {dest}. Re-run with --force to replace it.")
            remove_path(dest)

    (target_root / "plugins").mkdir(parents=True, exist_ok=True)
    (codex_home / "skills").mkdir(parents=True, exist_ok=True)
    install_plugin(paths["plugin"], plugin_dest, mode)
    copy_or_link(paths["skill"], skill_dest, mode)
    configure_hook_python(
        plugin_dest,
        python_executable or str(resolved(Path(sys.executable))),
    )


def target_marketplace_has_other_plugins(marketplace_path: Path) -> bool:
    if not marketplace_path.exists():
        return False
    payload = read_json(marketplace_path, "existing marketplace.json")
    plugins = payload.get("plugins")
    if plugins is None:
        return False
    if not isinstance(plugins, list):
        fail(f"existing marketplace.json field 'plugins' must be an array: {marketplace_path}")
    return any(
        not (isinstance(item, dict) and item.get("name") in OWNED_PLUGIN_IDS)
        for item in plugins
    )


def auto_review_marketplace_entry() -> dict[str, Any]:
    return {
        "name": PLUGIN_ID,
        "source": {"source": "local", "path": f"./plugins/{PLUGIN_DIR_NAME}"},
        "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }


def write_or_link_marketplace(
    paths: dict[str, Path],
    target_root: Path,
    mode: str,
) -> str:
    marketplace_path = target_root / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "symlink" and not target_marketplace_has_other_plugins(marketplace_path):
        if marketplace_path.exists() or marketplace_path.is_symlink():
            remove_path(marketplace_path)
        os.symlink(paths["marketplace"], marketplace_path)
        return marketplace_name(marketplace_path)

    if marketplace_path.exists() and not marketplace_path.is_symlink():
        payload = read_json(marketplace_path, "existing marketplace.json")
    else:
        payload = {
            "name": marketplace_name(paths["marketplace"]),
            "interface": {"displayName": "Local Plugins"},
            "plugins": [],
        }

    current_name = payload.get("name")
    if not isinstance(current_name, str) or not current_name.strip():
        payload["name"] = marketplace_name(paths["marketplace"])
    interface = payload.setdefault("interface", {})
    if not isinstance(interface, dict):
        payload["interface"] = {"displayName": "Local Plugins"}
    else:
        interface.setdefault("displayName", "Local Plugins")
    plugins = payload.setdefault("plugins", [])
    if not isinstance(plugins, list):
        fail(f"marketplace.json field 'plugins' must be an array: {marketplace_path}")
    payload["plugins"] = [
        item
        for item in plugins
        if not (isinstance(item, dict) and item.get("name") in OWNED_PLUGIN_IDS)
    ]
    payload["plugins"].append(auto_review_marketplace_entry())
    write_json(marketplace_path, payload)
    return marketplace_name(marketplace_path)


def without_auto_review_hooks(entries: Any) -> list[Any]:
    if not isinstance(entries, list):
        return []
    filtered: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            filtered.append(entry)
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            filtered.append(entry)
            continue
        kept = [
            hook
            for hook in hooks
            if not (
                isinstance(hook, dict)
                and "auto_review_hook.py" in str(hook.get("command", ""))
            )
        ]
        if len(kept) == len(hooks):
            filtered.append(entry)
        elif kept:
            updated = dict(entry)
            updated["hooks"] = kept
            filtered.append(updated)
    return filtered


def cleanup_legacy_global_hooks(codex_home: Path) -> None:
    hooks_path = codex_home / "hooks.json"
    if not hooks_path.exists():
        return
    payload = read_json(hooks_path, "existing hooks.json")
    hooks = payload.get("hooks", {})
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        fail(f"existing hooks.json field 'hooks' must be an object: {hooks_path}")

    cleaned_hooks = dict(hooks)
    for event_name in HOOK_EVENTS:
        cleaned_hooks[event_name] = without_auto_review_hooks(cleaned_hooks.get(event_name))
        if cleaned_hooks[event_name] == []:
            cleaned_hooks.pop(event_name)

    cleaned_payload = {"hooks": cleaned_hooks}
    if cleaned_payload != payload:
        write_json(hooks_path, cleaned_payload)


def cache_root(codex_home: Path, market_name: str, version: str) -> Path:
    return codex_home / "plugins" / "cache" / market_name / PLUGIN_ID / version


def link_cache_contents(plugin_root: Path, cache_dest: Path) -> None:
    if not cache_dest.is_dir():
        fail(f"Installed plugin cache was not found: {cache_dest}")
    for child in (".codex-plugin", "hooks", "tests"):
        dest = cache_dest / child
        source = plugin_root / child
        if not source.exists():
            continue
        if dest.exists() or dest.is_symlink():
            remove_path(dest)
        os.symlink(source, dest, target_is_directory=source.is_dir())


def start_app_server_reader(stream: TextIO) -> queue.Queue[str | None]:
    responses: queue.Queue[str | None] = queue.Queue()

    def read_lines() -> None:
        try:
            for line in stream:
                responses.put(line)
        finally:
            responses.put(None)

    threading.Thread(
        target=read_lines,
        name="auto-review-app-server-reader",
        daemon=True,
    ).start()
    return responses


def app_server_request(
    responses: queue.Queue[str | None],
    request_id: int,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            line = responses.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            fail(f"Codex app-server closed before responding to request {request_id}.")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            if "error" in payload:
                fail(f"Codex app-server request {request_id} failed: {payload['error']}")
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
    fail(f"Timed out waiting for Codex app-server response {request_id}.")


def trust_plugin_hooks(
    codex_home: Path,
    project_root: Path,
    selector_name: str,
    codex: str,
) -> None:
    process = subprocess.Popen(
        [codex, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=command_env(codex_home),
    )
    assert process.stdout is not None
    responses = start_app_server_reader(process.stdout)

    def send(payload: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "auto-review-installer", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        app_server_request(responses, 1)
        send({"jsonrpc": "2.0", "method": "initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(project_root)]},
            }
        )
        hooks_result = app_server_request(responses, 2)
        trust_entries: dict[str, dict[str, str]] = {}
        warnings: list[str] = []
        for entry in hooks_result.get("data") or []:
            if isinstance(entry, dict):
                warnings.extend(str(warning) for warning in entry.get("warnings") or [])
                for hook in entry.get("hooks") or []:
                    if not isinstance(hook, dict):
                        continue
                    key = hook.get("key")
                    current_hash = hook.get("currentHash")
                    if (
                        isinstance(key, str)
                        and key.startswith(f"{selector_name}:")
                        and isinstance(current_hash, str)
                    ):
                        trust_entries[key] = {"trusted_hash": current_hash}

        if warnings:
            fail("Codex reported hook warnings: " + "; ".join(warnings))
        if len(trust_entries) < 2:
            fail(
                f"Expected to discover at least 2 {selector_name} hooks, "
                f"found {len(trust_entries)}."
            )

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "config/batchWrite",
                "params": {
                    "edits": [
                        {
                            "keyPath": "hooks.state",
                            "value": trust_entries,
                            "mergeStrategy": "upsert",
                        }
                    ],
                    "reloadUserConfig": True,
                },
            }
        )
        app_server_request(responses, 3)

        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "hooks/list",
                "params": {"cwds": [str(project_root)]},
            }
        )
        verified = app_server_request(responses, 4)
        remaining_untrusted: list[str] = []
        for entry in verified.get("data") or []:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if (
                    isinstance(hook, dict)
                    and hook.get("key") in trust_entries
                    and hook.get("trustStatus") != "trusted"
                ):
                    remaining_untrusted.append(str(hook.get("key")))
        if remaining_untrusted:
            fail("Codex did not mark auto-review hooks trusted: " + ", ".join(remaining_untrusted))
    finally:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except OSError:
                pass


def install(args: argparse.Namespace) -> None:
    project_root = resolved(Path(args.project_root))
    target_root = Path(args.target_root).expanduser()
    codex_home = Path(args.codex_home).expanduser()
    paths = source_paths(project_root)
    check_source_layout(paths)
    codex = require_codex(codex_home)
    target_root.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)

    legacy_selector = f"{LEGACY_PLUGIN_ID}@{LEGACY_MARKETPLACE_NAME}"
    run_command(
        [codex, "plugin", "remove", legacy_selector],
        codex_home,
        allow_failure=True,
    )
    run_command(
        [codex, "plugin", "marketplace", "remove", LEGACY_MARKETPLACE_NAME],
        codex_home,
        allow_failure=True,
    )
    remove_config_sections(codex_home, LEGACY_PLUGIN_ID, LEGACY_MARKETPLACE_NAME)
    install_plugin_and_skill(
        paths,
        target_root,
        codex_home,
        args.mode,
        args.force,
        str(resolved(Path(sys.executable))),
    )
    market_name = write_or_link_marketplace(paths, target_root, args.mode)
    selector_name = f"{PLUGIN_ID}@{market_name}"
    cleanup_legacy_global_hooks(codex_home)

    run_command([codex, "plugin", "marketplace", "add", str(target_root)], codex_home)
    run_command([codex, "plugin", "add", selector_name], codex_home)

    version = plugin_version(paths["plugin"])
    if args.mode == "symlink":
        link_cache_contents(
            target_root / "plugins" / PLUGIN_DIR_NAME,
            cache_root(codex_home, market_name, version),
        )

    trust_plugin_hooks(codex_home, project_root, selector_name, codex)

    print(f"Installed {PRODUCT_NAME} using {args.mode} mode.")
    print(f"Plugin: {target_root / 'plugins' / PLUGIN_DIR_NAME}")
    print(f"Marketplace: {target_root / '.agents' / 'plugins' / 'marketplace.json'}")
    print(f"Plugin selector: {selector_name}")
    print(f"Hook trust: {codex_home / 'config.toml'}")
    print(f"Skill: {codex_home / 'skills' / SKILL_NAME}")
    print("Fully quit and restart Codex, then start a new task.")
    print("Existing tasks keep their previous hook set and cannot load this update.")
    print("Use standalone: $auto-review <your task>")
    print(
        "When bundled by Auto Workflow, use: "
        "$auto-workflow:auto-review <your task>"
    )


def remove_marketplace_entry(target_root: Path, keep_files: bool) -> None:
    marketplace_path = target_root / ".agents" / "plugins" / "marketplace.json"
    if not marketplace_path.exists() and not marketplace_path.is_symlink():
        return
    if marketplace_path.is_symlink():
        if not keep_files:
            marketplace_path.unlink()
        return
    payload = read_json(marketplace_path, "existing marketplace.json")
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        payload["plugins"] = [
            item
            for item in plugins
            if not (isinstance(item, dict) and item.get("name") in OWNED_PLUGIN_IDS)
        ]
        write_json(marketplace_path, payload)


def remove_config_sections(
    codex_home: Path,
    plugin_id: str,
    market_name: str,
    *,
    remove_marketplace: bool = True,
) -> None:
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return

    remove_headers = {f'[plugins."{plugin_id}@{market_name}"]'}
    if remove_marketplace:
        remove_headers.add(f"[marketplaces.{market_name}]")
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = (
                stripped in remove_headers
                or stripped.startswith(f'[hooks.state."{plugin_id}@{market_name}:')
                or stripped.startswith('[hooks.state."')
                and "auto_review_hook.py" in stripped
            )
        if not skipping:
            kept.append(line)

    if kept != lines:
        config_path.write_text("".join(kept), encoding="utf-8")


def uninstall(args: argparse.Namespace) -> None:
    project_root = resolved(Path(args.project_root))
    target_root = Path(args.target_root).expanduser()
    codex_home = Path(args.codex_home).expanduser()
    paths = source_paths(project_root)
    target_marketplace = target_root / ".agents" / "plugins" / "marketplace.json"
    if target_marketplace.exists() or target_marketplace.is_symlink():
        market_name = marketplace_name(target_marketplace)
    elif paths["marketplace"].exists():
        market_name = marketplace_name(paths["marketplace"])
    else:
        market_name = DEFAULT_MARKETPLACE_NAME
    marketplace_has_other_plugins = target_marketplace_has_other_plugins(target_marketplace)
    selector_name = f"{PLUGIN_ID}@{market_name}"
    legacy_selector = f"{LEGACY_PLUGIN_ID}@{LEGACY_MARKETPLACE_NAME}"

    codex = find_codex_command()
    if codex is not None:
        for selector in (selector_name, legacy_selector):
            run_command(
                [codex, "plugin", "remove", selector],
                codex_home,
                allow_failure=True,
            )
        marketplaces = [LEGACY_MARKETPLACE_NAME]
        if not marketplace_has_other_plugins:
            marketplaces.insert(0, market_name)
        for marketplace in dict.fromkeys(marketplaces):
            run_command(
                [codex, "plugin", "marketplace", "remove", marketplace],
                codex_home,
                allow_failure=True,
            )

    if codex_home.exists():
        cleanup_legacy_global_hooks(codex_home)
    remove_marketplace_entry(target_root, args.keep_files)

    if not args.keep_files:
        for path in (
            target_root / "plugins" / PLUGIN_DIR_NAME,
            codex_home / "skills" / SKILL_NAME,
            codex_home / "plugins" / "cache" / market_name / PLUGIN_ID,
            codex_home
            / "plugins"
            / "cache"
            / LEGACY_MARKETPLACE_NAME
            / LEGACY_PLUGIN_ID,
        ):
            if path.exists() or path.is_symlink():
                remove_path(path)

    remove_config_sections(
        codex_home,
        PLUGIN_ID,
        market_name,
        remove_marketplace=not marketplace_has_other_plugins,
    )
    remove_config_sections(codex_home, LEGACY_PLUGIN_ID, LEGACY_MARKETPLACE_NAME)
    print(f"Uninstalled {PRODUCT_NAME} from {target_root}.")
    print("Restart Codex to unload plugin hooks.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common: dict[str, Any] = {
        "project_root": {
            "default": str(Path(__file__).resolve().parents[1]),
            "help": "auto-review project root",
        },
        "target_root": {
            "default": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
            "help": "plugin marketplace/install root",
        },
        "codex_home": {
            "default": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
            "help": "Codex home directory",
        },
    }

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--project-root", **common["project_root"])
    install_parser.add_argument("--target-root", **common["target_root"])
    install_parser.add_argument("--codex-home", **common["codex_home"])
    install_parser.add_argument("--mode", choices=("symlink", "copy"), default="symlink")
    install_parser.add_argument("--force", action="store_true", default=True)
    install_parser.set_defaults(func=install)

    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--project-root", **common["project_root"])
    uninstall_parser.add_argument("--target-root", **common["target_root"])
    uninstall_parser.add_argument("--codex-home", **common["codex_home"])
    uninstall_parser.add_argument("--keep-files", action="store_true")
    uninstall_parser.set_defaults(func=uninstall)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
