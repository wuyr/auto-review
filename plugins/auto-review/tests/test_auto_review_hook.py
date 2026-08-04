#!/usr/bin/env python3
"""Unit tests for auto_review_hook.py."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_ROOT / "hooks" / "auto_review_hook.py"
HOOKS_CONFIG = PLUGIN_ROOT / "hooks" / "hooks.json"
REVIEW_PROMPT = "review本次修改，检查是否存在遗漏，逻辑错误等问题"
FIX_PROMPT = "修复这些问题然后重新做一次review"
REVIEW_SENTINEL = "<!-- auto-review:review -->"
FIX_SENTINEL = "<!-- auto-review:fix -->"
INLINE_FALLBACK_SENTINEL = "<!-- auto-review:inline-fallback -->"
CANONICAL_ACTIVATION = "$auto-workflow:auto-review"
LEGACY_ACTIVATION = "$auto-review"
PROPOSED_PLAN = "<proposed_plan>\n1. Inspect the code.\n2. Implement the fix.\n</proposed_plan>"


def local_hook_command(event_name: str) -> str:
    config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
    command = config["hooks"][event_name][0]["hooks"][0]["command"]
    if not command.startswith("python3 -X utf8 -c "):
        raise AssertionError(f"Unexpected hook command: {command}")
    executable = (
        subprocess.list2cmdline([sys.executable])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    return executable + " -X utf8 -c " + command[len("python3 -X utf8 -c ") :]


class AutoReviewHookTest(unittest.TestCase):
    def test_hook_protocol_forces_utf8_under_legacy_windows_code_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            env = os.environ.copy()
            env["AUTO_REVIEW_STATE_HOME"] = str(state_home)
            env["PYTHONUTF8"] = "0"
            env["PYTHONIOENCODING"] = "cp936"

            activation_prompt = f"{CANONICAL_ACTIVATION} 检查当前修改"
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = activation_prompt
            armed = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(armed.returncode, 0, armed.stderr.decode("utf-8"))

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(
                state["activation_prompt_sha256"],
                hashlib.sha256(activation_prompt.encode("utf-8")).hexdigest(),
            )

            payload = self.base_payload("Stop", state_home)
            stopped = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr.decode("utf-8"))
            raw_output = stopped.stdout
            self.assertNotIn(b"\xef\xbf\xbd", raw_output)
            block = json.loads(raw_output.decode("utf-8"))
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))

    def test_bootstrap_falls_back_when_loaded_cache_root_was_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_home = root / "state-home"
            workspace = root / "workspace"
            stable_hooks = root / "codex-home" / "plugins" / "auto-review" / "hooks"
            workspace.mkdir()
            stable_hooks.mkdir(parents=True)
            shutil.copy2(HOOK, stable_hooks / HOOK.name)

            command = local_hook_command("UserPromptSubmit")
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "stale-cache-session",
                "cwd": str(workspace),
                "prompt": f"{CANONICAL_ACTIVATION} check current changes",
            }
            env = os.environ.copy()
            env["CODEX_HOME"] = str(root / "codex-home")
            env["CODEX_PLUGIN_ROOT"] = str(root / "removed-cache-version")
            env["CLAUDE_PLUGIN_ROOT"] = str(root / "also-missing")
            env["AUTO_REVIEW_STATE_HOME"] = str(state_home)
            env["PYTHONIOENCODING"] = "utf-8"

            result = subprocess.run(
                command,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                env=env,
                cwd=workspace,
                shell=True,
                check=False,
                encoding="utf-8",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)
            self.assertTrue((state_home / "state" / "stale-cache-session.json").exists())

            stop_command = local_hook_command("Stop")
            stop_payload = {
                "hook_event_name": "Stop",
                "session_id": "stale-cache-session",
                "cwd": str(workspace),
            }
            stop_result = subprocess.run(
                stop_command,
                input=json.dumps(stop_payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                env=env,
                cwd=workspace,
                shell=True,
                check=False,
                encoding="utf-8",
            )

            self.assertEqual(stop_result.returncode, 0, stop_result.stderr)
            self.assertEqual(json.loads(stop_result.stdout)["decision"], "block")

    def run_hook(
        self,
        payload: dict,
        state_home: Path | None,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if state_home is None:
            env.pop("AUTO_REVIEW_STATE_HOME", None)
            env.pop("AUTO_REVIEW_LOOP_STATE_HOME", None)
        else:
            env["AUTO_REVIEW_STATE_HOME"] = str(state_home)
        if extra_env is not None:
            env.update(extra_env)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            encoding="utf-8",
        )

    def transcript(self, root: Path, assistant_text: str) -> Path:
        path = root / "transcript.jsonl"
        record = {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            }
        }
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def agent_message_record(self, text: str) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": text,
            },
        }

    def task_complete_record(self, text: str) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": text,
            },
        }

    def write_transcript(self, root: Path, records: list[dict]) -> Path:
        path = root / "transcript.jsonl"
        path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def goal_context_record(self, objective: str) -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<goal_context>\n"
                            "Continue working toward the active thread goal.\n"
                            f"<objective>\n{objective}\n</objective>\n"
                            "</goal_context>"
                        ),
                    }
                ],
            },
        }

    def goal_complete_record(self, status: str = "complete") -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_goal",
                "output": json.dumps({"goal": {"status": status}}, ensure_ascii=False),
            },
        }

    def goal_custom_tool_output_record(self, objective: str, status: str) -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_exec",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.0 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": "{}"},
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "goal": {
                                    "threadId": "session-1",
                                    "objective": objective,
                                    "status": status,
                                }
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            },
        }

    def goal_update_event_record(self, objective: str, status: str) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "thread_goal_updated",
                "threadId": "session-1",
                "goal": {
                    "threadId": "session-1",
                    "objective": objective,
                    "status": status,
                },
            },
        }

    def plan_item_record(self, text: str = "A completed plan.") -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "Plan",
                    "id": "plan-item-1",
                    "text": text,
                },
            },
        }

    def turn_context_record(self, mode: str) -> dict:
        return {
            "type": "turn_context",
            "payload": {
                "collaboration_mode": {
                    "mode": mode,
                },
            },
        }

    def developer_message_record(self, text: str) -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": text}],
            },
        }

    def state_file(self, state_home: Path, session_id: str = "session-1") -> Path:
        return state_home / "state" / f"{session_id}.json"

    def handoff_files(self, state_home: Path) -> list[Path]:
        return list((state_home / "deferred-plan").glob("*.json"))

    def base_payload(self, event: str, state_home: Path, session_id: str = "session-1") -> dict:
        return {
            "hook_event_name": event,
            "session_id": session_id,
            "cwd": str(state_home / "workspace"),
        }

    def arm_session(self, state_home: Path) -> None:
        payload = self.base_payload("UserPromptSubmit", state_home)
        payload["prompt"] = f"{CANONICAL_ACTIVATION} implement the task"
        result = self.run_hook(payload, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("auto_review_armed", result.stdout)

    def history_events(self, state_home: Path) -> list[dict]:
        history = state_home / "history.jsonl"
        if not history.exists():
            return []
        return [
            json.loads(line)
            for line in history.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def temp_fallback_state_home(self, cwd: Path) -> Path:
        digest = hashlib.sha256(str(cwd).encode()).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / "codex-auto-review" / digest

    def action_result(self, action: str, summary: str = "遗漏空状态") -> str:
        if action == "clean":
            payload = {"action": "clean", "issues": []}
        else:
            payload = {
                "action": action,
                "issues": [
                    {
                        "summary": summary,
                        "evidence": "screen.tsx 的空列表路径可达并显示空白页面",
                        "requirement_basis": "原始任务要求空状态可用",
                        "minimal_fix": "在现有组件内渲染 empty state",
                        "why_in_scope": "screen.tsx 由本任务直接修改",
                    }
                ],
            }
        return (
            "<auto_review_result>"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "</auto_review_result>"
        )

    def test_unrelated_namespaced_activation_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review:auto-review implement the task"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_legacy_activation_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = f"{LEGACY_ACTIVATION} implement the task"

            result = self.run_hook(payload, state_home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)
            self.assertTrue(self.state_file(state_home).exists())

    def enter_review_phase(self, state_home: Path) -> dict:
        self.arm_session(state_home)
        payload = self.base_payload("Stop", state_home)
        result = self.run_hook(payload, state_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        block = json.loads(result.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
        self.assertIn(REVIEW_SENTINEL, block["reason"])
        self.assertNotIn("systemMessage", block)
        self.assertNotIn("\n", block["reason"])
        self.assertIn("首次 discovery", block["reason"])
        self.assertIn("同一根因", block["reason"])
        self.assertIn("findings 没有最低数量", block["reason"])
        self.assertIn("不能自行升级为独立需求依据", block["reason"])
        self.assertIn("action=fix", block["reason"])
        self.assertIn("action=needs_replan", block["reason"])
        self.assertIn("删除新机制、回退原行为、局部修复和新增架构", block["reason"])
        self.assertLess(len(block["reason"]), 2200)
        return block

    def test_activation_arms_and_first_stop_emits_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 1)

    def test_activation_classifies_review_only_and_task_prompts(self) -> None:
        cases = (
            (CANONICAL_ACTIVATION, "existing_changes", True),
            (f"{CANONICAL_ACTIVATION} 检查一下", "existing_changes", True),
            ("$auto-review", "existing_changes", True),
            ("$auto-review 检查一下", "existing_changes", True),
            ("$auto-review review the current diff", "existing_changes", True),
            ("$auto-review 帮忙看看删除文件的逻辑有没有坑", "existing_changes", True),
            ("$auto-review 看看更新机制有没有坑", "existing_changes", True),
            (
                "$auto-review please take a look for regression in the remove flow",
                "existing_changes",
                True,
            ),
            (
                r"[$auto-review](C:\Users\tester\.codex\skills\auto-review\SKILL.md)",
                "existing_changes",
                True,
            ),
            (
                r"[$auto-review](C:\Users\tester\.codex\skills\auto-review\SKILL.md) 检查一下",
                "existing_changes",
                True,
            ),
            ("$auto-review 修复检查按钮", "task_changes", False),
            (f"{CANONICAL_ACTIVATION} 修复检查按钮", "task_changes", False),
            ("$auto-review implement the checker", "task_changes", False),
            ("$auto-review create a result file", "task_changes", False),
            ("$auto-review 帮我删除 review hook", "task_changes", False),
            ("$auto-review create a review report", "task_changes", False),
            (
                r"[$auto-review](C:\Users\tester\.codex\skills\auto-review\SKILL.md) 修复检查按钮",
                "task_changes",
                False,
            ),
            (
                r"[$auto-review](C:\Users\tester\.codex\skills\auto-review\SKILL.md) create a result file",
                "task_changes",
                False,
            ),
        )
        for index, (prompt, target_mode, allows_inline) in enumerate(cases):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as temp:
                state_home = Path(temp)
                payload = self.base_payload(
                    "UserPromptSubmit",
                    state_home,
                    session_id=f"session-{index}",
                )
                payload["prompt"] = prompt
                result = self.run_hook(payload, state_home)
                self.assertEqual(result.returncode, 0, result.stderr)
                state = json.loads(
                    self.state_file(state_home, f"session-{index}").read_text(encoding="utf-8")
                )
                self.assertEqual(state["review_target_mode"], target_mode)
                self.assertEqual(state["activation_allows_inline_review_result"], allows_inline)

    def test_bare_activation_skips_clean_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            workspace = state_home / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review"
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_skipped", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())

    def test_bare_activation_reviews_existing_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            workspace = state_home / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            (workspace / "change.txt").write_text("dirty\n", encoding="utf-8")

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review"
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertIn("当前未提交修改", block["reason"])
            self.assertIn("不推测需求遗漏", block["reason"])

    def test_bare_activation_plan_output_defers_before_clean_tree_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            workspace = state_home / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review"
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)

            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "实现计划"
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home, cwd=workspace)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertIn(
                f"激活 {CANONICAL_ACTIVATION} 的任务所产生的修改",
                block["reason"],
            )
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["review_target_mode"], "task_changes")

    def test_state_home_falls_back_to_git_dir_when_codex_home_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            git_dir = workspace / ".git"
            git_dir.mkdir()
            fake_home = root / "home-is-file"
            fake_home.write_text("not a directory", encoding="utf-8")

            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "cwd": str(workspace),
                "prompt": "$auto-review implement the task",
            }
            result = self.run_hook(
                payload,
                None,
                extra_env={"HOME": str(fake_home), "USERPROFILE": str(fake_home)},
                cwd=workspace,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)

            state_home = git_dir / "auto-review"
            self.assertTrue(self.state_file(state_home).exists())
            events = self.history_events(state_home)
            self.assertEqual(events[-1]["event"], "armed")

    def test_state_home_falls_back_to_portable_temp_dir_without_git(self) -> None:
        temp_parent = os.environ.get("PUBLIC") if os.name == "nt" else None
        with tempfile.TemporaryDirectory(dir=temp_parent) as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            fake_home = root / "home-is-file"
            fake_home.write_text("not a directory", encoding="utf-8")
            state_home = self.temp_fallback_state_home(workspace)
            shutil.rmtree(state_home, ignore_errors=True)

            try:
                payload = {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "cwd": str(workspace),
                    "prompt": "$auto-review implement the task",
                }
                result = self.run_hook(
                    payload,
                    None,
                    extra_env={"HOME": str(fake_home), "USERPROFILE": str(fake_home)},
                    cwd=workspace,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("auto_review_armed", result.stdout)
                self.assertTrue(self.state_file(state_home).exists())
                events = self.history_events(state_home)
                self.assertEqual(events[-1]["event"], "armed")
            finally:
                shutil.rmtree(state_home, ignore_errors=True)

    def test_inline_review_result_in_armed_turn_emits_fix_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review review本次会话中产生的修改"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)

            transcript = self.write_transcript(
                state_home,
                [
                    self.agent_message_record(
                        "发现 1 个问题。\n"
                        "<auto_review_result>\n"
                        '{"issues_found":true,"issues":[{"summary":"遗漏空状态","evidence":"screen.tsx 未处理空列表","fix_hint":"添加 empty state"}]}\n'
                        "</auto_review_result>"
                    )
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(FIX_PROMPT))
            self.assertIn(FIX_SENTINEL, block["reason"])
            self.assertIn("遗漏空状态", block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "fixing")
            self.assertEqual(state["fix_count"], 1)
            self.assertEqual(state["last_transition"], "armed_inline_review")
            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "inline_review_result")
            self.assertEqual(events[-1]["event"], "fix_prompt")
            self.assertEqual(events[-1]["source"], "armed_inline_review")

    def test_inline_review_result_is_not_suppressed_by_active_goal_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review 检查本次会话所修改的内容是否还有坑并修复"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现这个 goal，完成后自动 review。"),
                    self.goal_complete_record("active"),
                    self.agent_message_record(
                        "发现 1 个问题。\n"
                        "<auto_review_result>\n"
                        '{"issues_found":true,"issues":[{"summary":"策略未闭环","evidence":"policy.py 缺少边界处理","fix_hint":"补齐边界"}]}\n'
                        "</auto_review_result>"
                    ),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(FIX_PROMPT))
            self.assertIn(FIX_SENTINEL, block["reason"])
            self.assertIn("策略未闭环", block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "fixing")
            self.assertEqual(state["last_transition"], "armed_inline_review")
            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "inline_review_result")
            self.assertEqual(events[-1]["event"], "fix_prompt")

    def test_inline_clean_review_result_in_armed_turn_cleans_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review检查是否还存在遗漏"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)

            transcript = self.write_transcript(
                state_home,
                [
                    self.task_complete_record(
                        '未发现阻塞问题。\n<auto_review_result>{"issues_found":false,"issues":[]}</auto_review_result>'
                    )
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "inline_review_result")
            self.assertEqual(events[-1]["event"], "review_clean")
            self.assertEqual(events[-1]["source"], "armed_inline_review")

    def test_bare_activation_consumes_inline_structured_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)

            transcript = self.transcript(state_home, self.action_result("clean"))
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())
            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "inline_review_result")
            self.assertEqual(events[-1]["event"], "review_clean")

    def test_ordinary_armed_turn_does_not_consume_accidental_review_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(
                state_home,
                (
                    "Updated docs mention this example: "
                    '<auto_review_result>{"issues_found":false,"issues":[]}</auto_review_result>'
                ),
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 1)

    def test_inline_fallback_result_is_consumed_for_implementation_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(
                state_home,
                INLINE_FALLBACK_SENTINEL + self.action_result("clean"),
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "inline_review_result")
            self.assertEqual(events[-1]["event"], "review_clean")
            self.assertEqual(events[-1]["source"], "armed_inline_review")

    def test_stale_completed_goal_does_not_suppress_later_armed_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现旧 goal。"),
                    self.goal_complete_record("complete"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "$auto-review 检查本次会话修改是否还有坑",
                                }
                            ],
                        },
                    },
                    self.agent_message_record("我会检查当前修改。"),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "task_stop")

    def test_new_active_goal_after_completed_goal_still_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现旧 goal。"),
                    self.goal_complete_record("complete"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "/goal $auto-review 实现新 goal。",
                                }
                            ],
                        },
                    },
                    self.goal_context_record("$auto-review 实现新 goal。"),
                    self.goal_complete_record("active"),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "armed")

    def test_plan_output_defers_review_until_implementation_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, f"Here is the plan.\n{PROPOSED_PLAN}")

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")
            self.assertEqual(state["review_count"], 0)
            self.assertEqual(len(self.handoff_files(state_home)), 1)
            events = self.history_events(state_home)
            self.assertEqual([event["event"] for event in events], ["armed", "plan_deferred"])

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")
            self.assertEqual(state["review_count"], 0)
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "实现计划"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 1)
            self.assertEqual(state["last_transition"], "plan_implementation_stop")
            self.assertEqual(self.handoff_files(state_home), [])
            events = self.history_events(state_home)
            self.assertEqual(events[-1]["event"], "review_prompt")
            self.assertEqual(events[-1]["source"], "plan_implementation_stop")

    def test_plan_mode_waits_and_plan_item_defers_without_reviewing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("plan"),
                    self.agent_message_record("I am still gathering details for the plan."),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "armed")

            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("plan"),
                    self.plan_item_record("# Final implementation plan"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")
            self.assertEqual(state["review_count"], 0)
            self.assertEqual(len(self.handoff_files(state_home)), 1)

    def test_plan_mode_refinement_after_deferred_plan_preserves_review_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("plan"),
                    self.plan_item_record("# Initial implementation plan"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["collaboration_mode_kind"] = "plan"
            payload["prompt"] = (
                "补充：如果设备在冻结时间周期内重启，则开机后10分钟内只需要"
                "做一次补冻结，当成功重冻结后，主动退出轮询"
            )
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")
            self.assertEqual(state["last_transition"], "plan_refinement_prompt")
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("plan"),
                    self.plan_item_record("# Revised implementation plan"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["collaboration_mode_kind"] = "default"
            payload["prompt"] = "Implement the plan."
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("default"),
                    self.agent_message_record("Implemented the revised plan."),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            events = [event["event"] for event in self.history_events(state_home)]
            self.assertIn("plan_deferred_refinement", events)
            self.assertNotIn("plan_deferred_cancelled", events)

    def test_deferred_plan_same_session_default_mode_stop_runs_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            transcript = self.write_transcript(
                state_home,
                [
                    self.developer_message_record(
                        "<collaboration_mode># Collaboration Mode: Default\n\n"
                        "You are now in Default mode. Any previous instructions for other modes "
                        "(e.g. Plan mode) are no longer active.\n</collaboration_mode>"
                    ),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "plan_implementation_stop")

    def test_plan_mode_wait_then_default_implementation_stop_reviews_late_plan_item(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("plan"),
                    self.agent_message_record("The plan is not visible to the hook yet."),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "实现计划"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            transcript = self.write_transcript(
                state_home,
                [
                    self.turn_context_record("default"),
                    self.plan_item_record("# Previously completed plan"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "plan_implementation_stop")

    def test_deferred_plan_cancels_on_unrelated_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "Help me inspect an unrelated problem."
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())
            self.assertEqual(self.handoff_files(state_home), [])

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            events = self.history_events(state_home)
            self.assertEqual(events[-1]["event"], "plan_deferred_cancelled")

    def test_deferred_plan_keeps_state_on_implementation_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            for prompt in ("实现计划", "implement plan", "proceed with implementation"):
                payload = self.base_payload("UserPromptSubmit", state_home)
                payload["prompt"] = prompt
                result = self.run_hook(payload, state_home)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
                self.assertEqual(state["phase"], "deferred_after_plan")

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["last_transition"], "plan_implementation_stop")

    def test_deferred_plan_adopts_new_session_implementation_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(self.state_file(state_home, "session-1").exists())
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("UserPromptSubmit", state_home, session_id="session-2")
            payload["prompt"] = "实现计划"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home, "session-1").exists())
            self.assertEqual(self.handoff_files(state_home), [])

            state = json.loads(self.state_file(state_home, "session-2").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")
            self.assertEqual(state["adopted_from_state"], "session-1")
            self.assertEqual(state["last_transition"], "plan_implementation_prompt")

            payload = self.base_payload("Stop", state_home, session_id="session-2")
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            state = json.loads(self.state_file(state_home, "session-2").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "plan_implementation_stop")

            events = self.history_events(state_home)
            self.assertEqual([event["event"] for event in events[-3:]], [
                "plan_deferred_adopted",
                "plan_implementation_prompt",
                "review_prompt",
            ])
            self.assertEqual(events[-1]["source"], "plan_implementation_stop")

    def test_deferred_plan_new_session_unrelated_prompt_does_not_cancel_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            payload = self.base_payload("UserPromptSubmit", state_home, session_id="session-2")
            payload["prompt"] = "Explain the plan in more detail."
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(self.state_file(state_home, "session-1").exists())
            self.assertFalse(self.state_file(state_home, "session-2").exists())
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("Stop", state_home, session_id="session-2")
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(self.state_file(state_home, "session-1").exists())
            self.assertFalse(self.state_file(state_home, "session-2").exists())
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(self.state_file(state_home, "session-1").exists())
            self.assertEqual(len(self.handoff_files(state_home)), 1)
            events = self.history_events(state_home)
            self.assertEqual(events[-1]["event"], "plan_deferred")

    def test_deferred_plan_new_session_stop_can_adopt_handoff_with_implementation_transcript(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            transcript = self.write_transcript(
                state_home,
                [
                    {
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "A previous agent produced the plan below. "
                                        "Implement the plan in a fresh context."
                                    ),
                                }
                            ],
                        }
                    }
                ],
            )
            payload = self.base_payload("Stop", state_home, session_id="session-2")
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertFalse(self.state_file(state_home, "session-1").exists())
            self.assertEqual(self.handoff_files(state_home), [])

            state = json.loads(self.state_file(state_home, "session-2").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["adopted_from_state"], "session-1")
            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "plan_deferred_adopted")
            self.assertEqual(events[-1]["event"], "review_prompt")
            self.assertEqual(events[-1]["source"], "plan_implementation_stop")

    def test_goal_completion_late_arms_and_emits_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("实现这个目标，结束后主动触发 $auto-review。"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_skill_read",
                            "output": (
                                "Skill docs mention <auto_review_result>{}</auto_review_result> "
                                "but this is not an automatic review run."
                            ),
                        },
                    },
                    self.goal_complete_record("complete"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "目标已完成。"}],
                        },
                    },
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 1)
            self.assertEqual(state["last_transition"], "goal_complete_stop")
            self.assertEqual(state["activation_source"], "goal_complete")

            events = self.history_events(state_home)
            self.assertEqual(events[-2]["event"], "goal_armed_late")
            self.assertEqual(events[-1]["event"], "review_prompt")
            self.assertEqual(events[-1]["source"], "goal_complete_stop")

    def test_goal_completion_from_thread_goal_updated_event_emits_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_update_event_record(
                        "$auto-review 实现这个 0.134.0 goal，完成后自动 review。",
                        "active",
                    ),
                    self.goal_update_event_record(
                        "$auto-review 实现这个 0.134.0 goal，完成后自动 review。",
                        "complete",
                    ),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "目标已完成。"}],
                        },
                    },
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "goal_complete_stop")
            self.assertEqual(state["activation_source"], "goal_complete")

    def test_goal_completion_from_custom_tool_output_emits_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            objective = "$auto-review 按照方案落地修改。"
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_update_event_record(objective, "active"),
                    self.goal_custom_tool_output_record(objective, "complete"),
                    self.task_complete_record("目标已完成。"),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "goal_complete_stop")

    def test_goal_completion_uses_latest_goal_in_custom_tool_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            objective = "$auto-review 按照方案落地修改。"
            goal_output = self.goal_custom_tool_output_record(objective, "complete")
            goal_output["payload"]["output"].insert(
                -1,
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {
                            "goal": {
                                "threadId": "session-1",
                                "objective": objective,
                                "status": "active",
                            }
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_update_event_record(objective, "active"),
                    goal_output,
                    self.task_complete_record("目标已完成。"),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "goal_complete_stop")

    def test_goal_completion_ignores_json_embedded_in_custom_tool_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            objective = "$auto-review 完成这个目标。"
            completion = json.dumps(
                {"goal": {"objective": objective, "status": "complete"}},
                ensure_ascii=False,
            )
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_update_event_record(objective, "active"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call_exec",
                            "output": [
                                {
                                    "type": "input_text",
                                    "text": f"diagnostic log contained: {completion}",
                                }
                            ],
                        },
                    },
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_goal_completion_without_auto_review_request_does_not_emit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("实现这个目标。"),
                    self.goal_complete_record("complete"),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_goal_auto_review_waits_until_goal_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("实现这个目标，结束后主动触发 $auto-review。"),
                    self.goal_complete_record("active"),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_goal_command_prompt_does_not_arm_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = (
                f"/goal {CANONICAL_ACTIVATION} 实现这个目标，完成后自动 review。"
            )
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record(
                        f"实现这个目标，完成后自动触发 {CANONICAL_ACTIVATION}。"
                    ),
                    self.goal_complete_record("active"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record(
                        f"实现这个目标，完成后自动触发 {CANONICAL_ACTIVATION}。"
                    ),
                    self.goal_complete_record("complete"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))

    def test_goal_command_prompt_clears_deferred_plan_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(self.state_file(state_home).exists())
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "/goal $auto-review 实现新的目标，完成后自动 review。"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())
            self.assertEqual(self.handoff_files(state_home), [])

    def test_goal_inline_fallback_after_completion_does_not_schedule_duplicate_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现这个目标。"),
                    self.goal_complete_record("complete"),
                    self.agent_message_record(
                        INLINE_FALLBACK_SENTINEL + self.action_result("clean")
                    ),
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_goal_active_waits_even_with_deferred_plan_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现新的 goal。"),
                    self.goal_complete_record("active"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "deferred_after_plan")

    def test_goal_completion_takes_precedence_over_plan_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现新的 goal。"),
                    self.goal_complete_record("complete"),
                ],
            )
            payload = self.base_payload("Stop", state_home, session_id="session-2")
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            state = json.loads(self.state_file(state_home, "session-2").read_text(encoding="utf-8"))
            self.assertEqual(state["last_transition"], "goal_complete_stop")
            self.assertEqual(state["activation_source"], "goal_complete")
            self.assertFalse(self.state_file(state_home, "session-1").exists())
            self.assertEqual(self.handoff_files(state_home), [])

    def test_goal_active_takes_precedence_over_plan_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            transcript = self.transcript(state_home, PROPOSED_PLAN)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(self.handoff_files(state_home)), 1)

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现新的 goal。"),
                    self.goal_complete_record("active"),
                ],
            )
            payload = self.base_payload("Stop", state_home, session_id="session-2")
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home, "session-2").exists())
            self.assertTrue(self.state_file(state_home, "session-1").exists())
            self.assertEqual(len(self.handoff_files(state_home)), 1)

    def test_goal_objective_prompt_state_waits_until_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review 实现这个 goal objective。"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_armed", result.stdout)

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现这个 goal objective。"),
                    self.goal_complete_record("active"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "armed")

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("$auto-review 实现这个 goal objective。"),
                    self.goal_complete_record("complete"),
                ],
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "goal_complete_stop")
            self.assertEqual(state["activation_source"], "goal_complete")

    def test_goal_auto_review_does_not_rearm_after_existing_review_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("实现这个目标，结束后主动触发 $auto-review。"),
                    self.goal_complete_record("complete"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": REVIEW_SENTINEL}],
                        },
                    },
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_goal_auto_review_ignores_review_activity_before_latest_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": REVIEW_SENTINEL}],
                        },
                    },
                    self.goal_context_record("实现新的目标，结束后主动触发 $auto-review。"),
                    self.goal_complete_record("complete"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "目标已完成。"}],
                        },
                    },
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertEqual(block["decision"], "block")
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])

            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["last_transition"], "goal_complete_stop")

    def test_goal_auto_review_does_not_replay_on_later_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("实现这个目标，结束后主动触发 $auto-review。"),
                    self.goal_complete_record("complete"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "目标已完成。"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "解释一下刚才的结果。"}],
                        },
                    },
                ],
            )

            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_auto_submitted_prompt_does_not_arm_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = f"{REVIEW_PROMPT}\n\n{REVIEW_SENTINEL}\nauto-review"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_clean_review_deletes_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(
                state_home,
                '未发现阻塞问题。\n<auto_review_result>\n{"issues_found":false,"issues":[]}\n</auto_review_result>',
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_review_with_issues_emits_fix_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(
                state_home,
                (
                    "发现 1 个问题。\n"
                    "<auto_review_result>\n"
                    '{"issues_found":true,"issues":[{"summary":"遗漏空状态","evidence":"screen.tsx 未处理空列表","fix_hint":"添加 empty state"}]}\n'
                    "</auto_review_result>"
                ),
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(FIX_PROMPT))
            self.assertIn(FIX_SENTINEL, block["reason"])
            self.assertIn("遗漏空状态", block["reason"])
            self.assertIn("删除新机制、回退原行为、局部修复、新增架构", block["reason"])
            self.assertIn("仅处理同一根因", block["reason"])
            self.assertIn("不存在范围内的最小修复", block["reason"])
            self.assertIn("下一轮由 model 判定 needs_replan", block["reason"])
            self.assertNotIn("systemMessage", block)
            self.assertNotIn("\n", block["reason"])
            self.assertNotIn("```json", block["reason"])
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "fixing")
            self.assertEqual(state["fix_count"], 1)

    def test_model_fix_action_emits_minimal_fix_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(state_home, self.action_result("fix"))
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(FIX_PROMPT))
            self.assertIn("原始任务要求空状态可用", block["reason"])
            self.assertIn("在现有组件内渲染 empty state", block["reason"])
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "fixing")
            self.assertEqual(len(state["seen_issue_fingerprints"]), 1)

    def test_model_needs_replan_stops_without_marking_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(
                state_home,
                self.action_result("needs_replan", summary="需要改变公开状态协议"),
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_needs_replan", result.stdout)
            self.assertIn("without marking the review clean", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())
            self.assertEqual(self.history_events(state_home)[-1]["event"], "review_needs_replan")

    def test_inline_fix_then_clean_closure_stops_without_second_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)

            transcript = self.transcript(state_home, self.action_result("fix"))
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(FIX_PROMPT, result.stdout)

            state_path = self.state_file(state_home)
            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertIn("定向 closure", block["reason"])
            self.assertIn("修复是否破坏原始需求或跨边界合同", block["reason"])

            transcript = self.transcript(state_home, self.action_result("clean"))
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(state_path.exists())
            events = self.history_events(state_home)
            self.assertEqual(events[-1]["event"], "review_clean")
            self.assertEqual(events[-1]["review_stage"], "closure")

    def test_repeated_exact_finding_pauses_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            marker = self.action_result("fix")
            transcript = self.transcript(state_home, marker)
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(FIX_PROMPT, result.stdout)

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("定向 closure", json.loads(result.stdout)["reason"])

            transcript = self.transcript(state_home, marker)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_paused", result.stdout)
            self.assertIn("repeated_exact_finding", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())

    def test_automatic_fix_budget_pauses_instead_of_classifying(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            state_path = self.state_file(state_home)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["fix_count"] = 2
            state["seen_issue_fingerprints"] = []
            state_path.write_text(json.dumps(state), encoding="utf-8")

            transcript = self.transcript(
                state_home,
                self.action_result("fix", summary="第二个独立问题"),
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto_review_paused", result.stdout)
            self.assertIn("automatic_fix_budget_exhausted", result.stdout)
            self.assertNotIn("needs_replan", result.stdout)
            self.assertFalse(state_path.exists())

    def test_new_action_requires_model_semantic_basis_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(
                state_home,
                '<auto_review_result>{"action":"fix","issues":[{"summary":"问题",'
                '"evidence":"可达错误","minimal_fix":"局部修复"}]}</auto_review_result>',
            )
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("review_result_invalid", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())

    def test_review_result_can_be_read_from_last_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            payload = self.base_payload("Stop", state_home)
            payload["last_assistant_message"] = (
                '未发现阻塞问题。\n<auto_review_result>\n{"issues_found":false,"issues":[]}\n</auto_review_result>'
            )
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

    def test_fix_stop_emits_next_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            state_path = self.state_file(state_home)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["phase"] = "fixing"
            state["fix_count"] = 1
            state_path.write_text(json.dumps(state), encoding="utf-8")

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(result.stdout)
            self.assertTrue(block["reason"].startswith(REVIEW_PROMPT))
            self.assertIn(REVIEW_SENTINEL, block["reason"])
            self.assertIn("定向 closure", block["reason"])
            self.assertIn("不要重新开放对全部累计修改的通用 hunting", block["reason"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 2)
            self.assertEqual(state["review_stage"], "closure")

    def test_subagent_stop_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            payload = self.base_payload("SubagentStop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "armed")

    def test_parent_session_stop_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            payload = self.base_payload("Stop", state_home)
            payload["parent_session_id"] = "parent-session"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "armed")

    def test_invalid_review_result_pauses_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(state_home, "发现问题：这里没有结构化结果。")
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0)
            self.assertIn("marker missing or invalid", result.stderr)
            self.assertIn("auto_review_paused", result.stdout)
            self.assertIn("review_result_invalid", result.stdout)
            self.assertFalse(self.state_file(state_home).exists())

    def test_bad_state_timestamp_fails_open_and_cleans_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.arm_session(state_home)
            state_path = self.state_file(state_home)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["updated_at"] = "not-a-timestamp"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            payload = self.base_payload("Stop", state_home)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(state_path.exists())

            history = (state_home / "history.jsonl").read_text(encoding="utf-8")
            self.assertIn("state_bad_timestamp", history)


if __name__ == "__main__":
    unittest.main()
