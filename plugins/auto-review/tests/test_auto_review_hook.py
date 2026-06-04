#!/usr/bin/env python3
"""Unit tests for auto_review_hook.py."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_ROOT / "hooks" / "auto_review_hook.py"
REVIEW_PROMPT = "review本次修改，检查是否存在遗漏，逻辑错误等问题"
FIX_PROMPT = "修复这些问题然后重新做一次review"
REVIEW_SENTINEL = "<!-- auto-review:review -->"
FIX_SENTINEL = "<!-- auto-review:fix -->"
PROPOSED_PLAN = "<proposed_plan>\n1. Inspect the code.\n2. Implement the fix.\n</proposed_plan>"


class AutoReviewHookTest(unittest.TestCase):
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
        payload["prompt"] = "$auto-review implement the task"
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

    def test_namespaced_activation_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            payload = self.base_payload("UserPromptSubmit", state_home)
            payload["prompt"] = "$auto-review:auto-review implement the task"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

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
        self.assertLess(len(block["reason"]), 700)
        return block

    def test_activation_arms_and_first_stop_emits_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 1)

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
        with tempfile.TemporaryDirectory() as temp:
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
            payload["prompt"] = "/goal $auto-review 实现这个目标，完成后自动 review。"
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())

            transcript = self.write_transcript(
                state_home,
                [
                    self.goal_context_record("实现这个目标，完成后自动触发 $auto-review。"),
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
                    self.goal_context_record("实现这个目标，完成后自动触发 $auto-review。"),
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
            self.assertEqual(result.stdout, "")
            self.assertFalse(self.state_file(state_home).exists())
            self.assertEqual(self.handoff_files(state_home), [])

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
            self.assertNotIn("systemMessage", block)
            self.assertNotIn("\n", block["reason"])
            self.assertNotIn("```json", block["reason"])
            state = json.loads(self.state_file(state_home).read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "fixing")
            self.assertEqual(state["fix_count"], 1)

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
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["review_count"], 2)

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

    def test_invalid_review_result_cleans_state_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_home = Path(temp)
            self.enter_review_phase(state_home)
            transcript = self.transcript(state_home, "发现问题：这里没有结构化结果。")
            payload = self.base_payload("Stop", state_home)
            payload["transcript_path"] = str(transcript)
            result = self.run_hook(payload, state_home)
            self.assertEqual(result.returncode, 0)
            self.assertIn("marker missing or invalid", result.stderr)
            self.assertEqual(result.stdout, "")
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
