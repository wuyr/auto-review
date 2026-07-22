#!/usr/bin/env python3
"""Codex hook entrypoint for the auto-review plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REVIEW_PROMPT = "review本次修改，检查是否存在遗漏，逻辑错误等问题"
FIX_PROMPT = "修复这些问题然后重新做一次review"
REVIEW_SENTINEL = "<!-- auto-review:review -->"
FIX_SENTINEL = "<!-- auto-review:fix -->"
RESULT_RE = re.compile(
    r"<auto_review_result>\s*(\{.*?\})\s*</auto_review_result>",
    re.DOTALL,
)
PROPOSED_PLAN_RE = re.compile(r"<\s*proposed_plan(?:\s[^>]*)?>", re.IGNORECASE)
GOAL_CONTEXT_RE = re.compile(r"<\s*goal_context(?:\s[^>]*)?>", re.IGNORECASE)
GOAL_COMMAND_RE = re.compile(r"^\s*/goal(?:\s|$)", re.IGNORECASE)
GOAL_OBJECTIVE_RE = re.compile(
    r"<\s*(?:objective|untrusted_objective)\s*>(.*?)</\s*(?:objective|untrusted_objective)\s*>",
    re.DOTALL | re.IGNORECASE,
)
STATE_TTL_SECONDS = 12 * 60 * 60
MAX_AUTOMATIC_FIXES = 2
ACTION_CLEAN = "clean"
ACTION_FIX = "fix"
ACTION_NEEDS_REPLAN = "needs_replan"
REVIEW_STAGE_DISCOVERY = "discovery"
REVIEW_STAGE_CLOSURE = "closure"
TARGET_TASK_CHANGES = "task_changes"
TARGET_EXISTING_CHANGES = "existing_changes"
_CURRENT_PAYLOAD: dict[str, Any] | None = None
_STATE_BASE_CACHE: dict[str, Path] = {}


def portable_home() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def resolved_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        try:
            return path.expanduser().absolute()
        except RuntimeError:
            return path.absolute()


def git_dir_for_cwd(cwd: str) -> Path | None:
    current = resolved_path(Path(cwd or os.getcwd()))
    if current.is_file():
        current = current.parent

    for directory in (current, *current.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return dot_git
        if not dot_git.is_file():
            continue

        try:
            raw = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        match = re.match(r"gitdir:\s*(.+)", raw, re.IGNORECASE)
        if not match:
            continue
        git_dir = Path(match.group(1).strip())
        if not git_dir.is_absolute():
            git_dir = directory / git_dir
        return resolved_path(git_dir)
    return None


def can_use_state_base(path: Path) -> bool:
    probe: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}-{time.time_ns()}"
        probe.write_text("", encoding="utf-8")
        return True
    except (OSError, RuntimeError):
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def temp_state_base_for_cwd(cwd: str) -> Path:
    digest = hashlib.sha256(cwd.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "codex-auto-review" / digest


def state_base(payload: dict[str, Any] | None = None) -> Path:
    override = os.environ.get("AUTO_REVIEW_STATE_HOME") or os.environ.get(
        "AUTO_REVIEW_LOOP_STATE_HOME"
    )
    if override:
        return Path(override).expanduser()

    effective_payload = payload or _CURRENT_PAYLOAD or {}
    cwd = cwd_from_payload(effective_payload)
    cached = _STATE_BASE_CACHE.get(cwd)
    if cached is not None:
        return cached

    candidates: list[Path] = []
    home = portable_home()
    if home is not None:
        candidates.append(home / ".codex" / "auto-review")

    git_dir = git_dir_for_cwd(cwd)
    if git_dir is not None:
        candidates.append(git_dir / "auto-review")

    candidates.append(temp_state_base_for_cwd(cwd))
    for candidate in candidates:
        if can_use_state_base(candidate):
            _STATE_BASE_CACHE[cwd] = candidate
            return candidate

    fallback = candidates[-1]
    _STATE_BASE_CACHE[cwd] = fallback
    return fallback


def now() -> int:
    return int(time.time())


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def cwd_from_payload(payload: dict[str, Any]) -> str:
    for key in ("cwd", "workspace", "current_dir"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return os.getcwd()


def state_key(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        key = session_id
    else:
        digest = hashlib.sha256(cwd_from_payload(payload).encode()).hexdigest()[:16]
        key = f"cwd-{digest}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]


def cwd_key(payload: dict[str, Any]) -> str:
    return hashlib.sha256(cwd_from_payload(payload).encode()).hexdigest()[:16]


def state_path_for_key(key: str, payload: dict[str, Any] | None = None) -> Path:
    return state_base(payload) / "state" / f"{key}.json"


def state_path(payload: dict[str, Any]) -> Path:
    return state_path_for_key(state_key(payload), payload)


def plan_handoff_path(payload: dict[str, Any]) -> Path:
    return state_base(payload) / "deferred-plan" / f"{cwd_key(payload)}.json"


def history_path(payload: dict[str, Any] | None = None) -> Path:
    return state_base(payload) / "history.jsonl"


def debug_path(payload: dict[str, Any] | None = None) -> Path:
    return state_base(payload) / "debug.jsonl"


def debug_enabled() -> bool:
    return os.environ.get("AUTO_REVIEW_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_history(event: dict[str, Any]) -> None:
    event.setdefault("ts", now())
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def append_debug(event: dict[str, Any]) -> None:
    if not debug_enabled():
        return
    event.setdefault("ts", now())
    path = debug_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def parse_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    path = state_path(payload)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cleanup_state(payload)
        return None
    if not isinstance(state, dict):
        cleanup_state(payload)
        return None
    timestamp_value = state.get("updated_at") or state.get("created_at")
    updated_at = parse_timestamp(timestamp_value)
    if timestamp_value not in (None, "") and updated_at is None:
        append_history({"event": "state_bad_timestamp", "state": state_key(payload)})
        cleanup_state(payload)
        return None
    if updated_at and now() - updated_at > STATE_TTL_SECONDS:
        append_history({"event": "state_stale", "state": state_key(payload)})
        cleanup_state(payload)
        return None
    return state


def load_plan_handoff(payload: dict[str, Any]) -> dict[str, Any] | None:
    path = plan_handoff_path(payload)
    if not path.exists():
        return None
    try:
        handoff = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cleanup_plan_handoff(payload)
        return None
    if not isinstance(handoff, dict):
        cleanup_plan_handoff(payload)
        return None
    timestamp_value = handoff.get("updated_at") or handoff.get("created_at")
    updated_at = parse_timestamp(timestamp_value)
    origin_state = str(handoff.get("origin_state_key") or "")
    if timestamp_value not in (None, "") and updated_at is None:
        append_history(
            {
                "event": "plan_handoff_bad_timestamp",
                "state": origin_state or state_key(payload),
            }
        )
        cleanup_plan_handoff(payload, cleanup_origin_state=True)
        return None
    if updated_at and now() - updated_at > STATE_TTL_SECONDS:
        append_history(
            {
                "event": "plan_handoff_stale",
                "state": origin_state or state_key(payload),
            }
        )
        cleanup_plan_handoff(payload, cleanup_origin_state=True)
        return None
    return handoff


def save_state(payload: dict[str, Any], state: dict[str, Any]) -> None:
    timestamp = now()
    state.setdefault("created_at", timestamp)
    state["updated_at"] = timestamp
    state["session_id"] = str(payload.get("session_id") or "")
    state["cwd"] = cwd_from_payload(payload)
    write_json_atomic(state_path(payload), state)


def save_plan_handoff(payload: dict[str, Any], state: dict[str, Any]) -> None:
    timestamp = now()
    handoff = dict(state)
    handoff["phase"] = "deferred_after_plan"
    handoff["origin_state_key"] = state_key(payload)
    handoff["origin_session_id"] = str(payload.get("session_id") or "")
    handoff["cwd"] = cwd_from_payload(payload)
    handoff.setdefault("created_at", timestamp)
    handoff["updated_at"] = timestamp
    write_json_atomic(plan_handoff_path(payload), handoff)


def plan_handoff_origin_state(handoff: dict[str, Any] | None) -> str:
    if not isinstance(handoff, dict):
        return ""
    return str(handoff.get("origin_state_key") or "")


def payload_owns_plan_handoff(payload: dict[str, Any], handoff: dict[str, Any] | None) -> bool:
    origin_state = plan_handoff_origin_state(handoff)
    return bool(origin_state and origin_state == state_key(payload))


def cleanup_state(payload: dict[str, Any]) -> None:
    cleanup_state_key(state_key(payload), payload)


def cleanup_state_key(key: str, payload: dict[str, Any] | None = None) -> None:
    if not key:
        return
    try:
        state_path_for_key(key, payload).unlink()
    except FileNotFoundError:
        pass


def cleanup_plan_handoff(payload: dict[str, Any], cleanup_origin_state: bool = False) -> None:
    origin_state = ""
    path = plan_handoff_path(payload)
    if cleanup_origin_state and path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            origin_state = str(value.get("origin_state_key") or "")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if cleanup_origin_state:
        cleanup_state_key(origin_state, payload)


def text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := text_from_value(item)))
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), str):
            return value["content"]
        if isinstance(value.get("content"), list):
            return text_from_value(value["content"])
    return ""


def prompt_from_payload(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "message", "input"):
        text = text_from_value(payload.get(key))
        if text:
            return text
    return ""


def should_arm(prompt: str) -> bool:
    if not prompt:
        return False
    if REVIEW_SENTINEL in prompt or FIX_SENTINEL in prompt:
        return False
    return bool(re.search(r"\$auto-review(?:\s|$|[^A-Za-z0-9_:-])", prompt, re.IGNORECASE))


def looks_like_inline_review_intent(prompt: str) -> bool:
    text = re.sub(
        r"\$auto-review(?=\s|$|[^A-Za-z0-9_:-])",
        " ",
        prompt or "",
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized:
        return False

    english_patterns = (
        r"\breview\b",
        r"\bcode\s+review\b",
        r"\bcheck\b",
        r"\binspect\b",
        r"\baudit\b",
        r"\bmissing\b",
        r"\blogic\s+error",
        r"\bregression\b",
    )
    if any(re.search(pattern, normalized) for pattern in english_patterns):
        return True

    chinese_phrases = (
        "review",
        "检查",
        "复查",
        "审查",
        "遗漏",
        "逻辑错误",
        "回归",
        "测试缺口",
        "坑",
        "闭环",
    )
    return any(phrase in text for phrase in chinese_phrases)


def prompt_without_activation(prompt: str) -> str:
    text = re.sub(
        r"\$auto-review(?=\s|$|[^A-Za-z0-9_:-])",
        " ",
        prompt or "",
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip()


def looks_like_review_only_prompt(prompt: str) -> bool:
    body = prompt_without_activation(prompt)
    if not body:
        return True
    normalized = body.lower()
    if re.search(r"^(?:please\s+)?(?:review|check|inspect|audit)\b", normalized):
        return True
    if re.search(r"^(?:请|帮我|麻烦)?(?:检查|复查|审查|review)", body, re.IGNORECASE):
        return True
    change_intent = re.search(
        r"\b(?:implement|fix|add|update|refactor|build)\b|实现|修复|新增|添加|修改|重构|开发",
        normalized,
    )
    return not change_intent and looks_like_inline_review_intent(prompt)


def review_target_for_prompt(prompt: str) -> tuple[str, bool]:
    review_only = looks_like_review_only_prompt(prompt)
    if review_only:
        return TARGET_EXISTING_CHANGES, True
    return TARGET_TASK_CHANGES, False


def git_worktree_has_changes(cwd: str) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def has_auto_review_sentinel(prompt: str) -> bool:
    return REVIEW_SENTINEL in prompt or FIX_SENTINEL in prompt


def is_proposed_plan_output(text: str) -> bool:
    return bool(PROPOSED_PLAN_RE.search(text or ""))


def normalized_collaboration_mode(value: Any) -> str:
    if isinstance(value, dict):
        mode = normalized_collaboration_mode(value.get("mode"))
        if mode:
            return mode
        return normalized_collaboration_mode(value.get("kind"))
    text = str(value or "").strip().lower()
    if text in {"plan", "default"}:
        return text
    if "collaboration_mode" in text or "collaboration mode" in text:
        if (
            "# collaboration mode: default" in text
            or "you are now in default mode" in text
            or text.startswith("default mode")
        ):
            return "default"
        if (
            "# plan mode" in text
            or text.startswith("plan mode")
            or "you are in **plan mode**" in text
        ):
            return "plan"
    return ""


def collaboration_mode_from_record(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if isinstance(payload, dict):
        mode = normalized_collaboration_mode(payload.get("collaboration_mode_kind"))
        if mode:
            return mode
        mode = normalized_collaboration_mode(payload.get("collaboration_mode"))
        if mode:
            return mode

    text = text_from_record(record)
    if text and "<collaboration_mode>" in text:
        return normalized_collaboration_mode(text)
    return ""


def latest_collaboration_mode(payload: dict[str, Any]) -> str:
    for key in ("collaboration_mode_kind", "collaboration_mode", "mode"):
        mode = normalized_collaboration_mode(payload.get(key))
        if mode:
            return mode

    latest_mode = ""
    for record in transcript_records(payload):
        mode = collaboration_mode_from_record(record)
        if mode:
            latest_mode = mode
    return latest_mode


def is_plan_item_record(record: dict[str, Any]) -> bool:
    item = record.get("item")
    if isinstance(item, dict) and str(item.get("type") or "").lower() == "plan":
        return True

    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    item = payload.get("item")
    return isinstance(item, dict) and str(item.get("type") or "").lower() == "plan"


def has_plan_output(payload: dict[str, Any]) -> bool:
    if is_proposed_plan_output(last_assistant_text(payload)):
        return True
    return any(is_plan_item_record(record) for record in transcript_records(payload))


def is_goal_workflow_prompt(prompt: str) -> bool:
    text = prompt or ""
    return bool(GOAL_COMMAND_RE.search(text) or GOAL_CONTEXT_RE.search(text))


def looks_like_plan_implementation_intent(prompt: str) -> bool:
    text = re.sub(r"\s+", " ", prompt or "").strip().lower()
    if not text:
        return False

    english_patterns = (
        r"\bimplement(?:\s+the)?\s+plan\b",
        r"\bproceed\s+with\s+implementation\b",
        r"\bproceed\s+to\s+implementation\b",
        r"\bstart\s+(?:the\s+)?implementation\b",
        r"\bcontinue\s+with\s+implementation\b",
        r"\bexecute(?:\s+the)?\s+plan\b",
        r"\bcarry\s+out(?:\s+the)?\s+plan\b",
    )
    if any(re.search(pattern, text) for pattern in english_patterns):
        return True

    chinese_phrases = (
        "实现计划",
        "按计划实现",
        "按照计划实现",
        "执行计划",
        "开始实现",
        "开始实施",
        "继续实现",
        "继续实施",
        "根据计划实现",
        "基于计划实现",
        "按上面的计划",
        "按这个计划",
        "实现这个计划",
        "实施这个计划",
    )
    return any(phrase in prompt for phrase in chinese_phrases)


def looks_like_plan_refinement_intent(prompt: str) -> bool:
    text = re.sub(r"\s+", " ", prompt or "").strip().lower()
    if not text:
        return False

    english_patterns = (
        r"\badd(?:itional)?\s+(?:requirement|constraint|detail|note)s?\b",
        r"\bone\s+more\s+(?:requirement|constraint|detail|note)\b",
        r"\bmore\s+(?:requirement|constraint|detail|note)s?\b",
        r"\bnew\s+(?:requirement|constraint|detail|note)s?\b",
        r"\brevise(?:\s+the)?\s+plan\b",
        r"\bupdate(?:\s+the)?\s+plan\b",
        r"\bmodify(?:\s+the)?\s+plan\b",
        r"\bchange(?:\s+the)?\s+plan\b",
        r"\badjust(?:\s+the)?\s+plan\b",
        r"\brefine(?:\s+the)?\s+plan\b",
        r"\bclarif(?:y|ication)\b",
    )
    if any(re.search(pattern, text) for pattern in english_patterns):
        return True

    chinese_phrases = (
        "补充",
        "追加",
        "新增要求",
        "增加要求",
        "新要求",
        "再加一个要求",
        "再加一条",
        "另外补充",
        "还有一个要求",
        "还有一条",
        "改一下计划",
        "修改计划",
        "调整计划",
        "更新计划",
        "完善计划",
        "细化计划",
    )
    return any(phrase in prompt for phrase in chinese_phrases)


def assistant_text_from_record(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return text_from_value(message.get("content"))

    payload = record.get("payload")
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "agent_message":
            return text_from_value(payload.get("message"))
        if payload_type == "task_complete":
            return text_from_value(payload.get("last_agent_message"))

        payload_message = payload.get("message")
        if isinstance(payload_message, dict) and payload_message.get("role") == "assistant":
            return text_from_value(payload_message.get("content"))
        if payload.get("role") == "assistant":
            return text_from_value(payload.get("content"))

    if record.get("role") == "assistant":
        return text_from_value(record.get("content"))
    return ""


def text_from_record(record: dict[str, Any]) -> str:
    values: list[str] = []
    direct = text_from_value(record.get("content"))
    if direct:
        values.append(direct)

    message = record.get("message")
    if isinstance(message, dict):
        text = text_from_value(message.get("content"))
        if text:
            values.append(text)

    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("content", "message", "last_agent_message", "last_assistant_message"):
            text = text_from_value(payload.get(key))
            if text:
                values.append(text)
        output = payload.get("output")
        if isinstance(output, str) and output:
            values.append(output)

    if not values:
        return ""
    return "\n".join(dict.fromkeys(values))


def transcript_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def record_role(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, dict) and isinstance(message.get("role"), str):
        return message["role"]

    payload = record.get("payload")
    if isinstance(payload, dict):
        payload_message = payload.get("message")
        if isinstance(payload_message, dict) and isinstance(payload_message.get("role"), str):
            return payload_message["role"]
        if isinstance(payload.get("role"), str):
            return payload["role"]

    if isinstance(record.get("role"), str):
        return record["role"]
    return ""


def is_user_supplied_record(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "user_message":
        return True
    return record_role(record) == "user"


def latest_user_supplied_text(payload: dict[str, Any]) -> str:
    last_text = ""
    for record in transcript_records(payload):
        if is_user_supplied_record(record):
            text = text_from_record(record)
            if text:
                last_text = text
    return last_text


def stop_payload_has_plan_implementation_intent(payload: dict[str, Any]) -> bool:
    prompt = prompt_from_payload(payload)
    if looks_like_plan_implementation_intent(prompt):
        return True
    return looks_like_plan_implementation_intent(latest_user_supplied_text(payload))


def should_adopt_plan_handoff_on_stop(
    payload: dict[str, Any],
    handoff: dict[str, Any],
) -> bool:
    return stop_payload_has_plan_implementation_intent(payload)


def last_assistant_text(payload: dict[str, Any]) -> str:
    direct = text_from_value(payload.get("last_assistant_message"))
    if direct:
        return direct

    inline = text_from_value(payload.get("assistant_output"))
    if inline:
        return inline

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    path = Path(transcript_path)
    if not path.exists():
        return ""

    last_text = ""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    text = assistant_text_from_record(record)
                    if text:
                        last_text = text
    except OSError:
        return ""
    return last_text


def goal_objectives_from_text(text: str) -> list[str]:
    objectives: list[str] = []
    if not GOAL_CONTEXT_RE.search(text or ""):
        return objectives
    for match in GOAL_OBJECTIVE_RE.finditer(text):
        objective = match.group(1).strip()
        if objective:
            objectives.append(objective)
    return objectives


def goal_command_objective_from_text(text: str) -> str:
    match = GOAL_COMMAND_RE.search(text or "")
    if not match:
        return ""
    return text[match.end() :].strip()


def parsed_goal_from_json_text(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    goal = value.get("goal")
    return goal if isinstance(goal, dict) else None


def parsed_goal_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload")
    if isinstance(payload, dict):
        goal = payload.get("goal")
        if isinstance(goal, dict):
            return goal

        payload_type = payload.get("type")
        output = payload.get("output")
        if payload_type == "function_call_output" and isinstance(output, str):
            goal = parsed_goal_from_json_text(output)
            if goal is not None:
                return goal
        if payload_type == "custom_tool_call_output" and isinstance(output, list):
            last_goal: dict[str, Any] | None = None
            for block in output:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                goal = parsed_goal_from_json_text(text)
                if goal is not None:
                    last_goal = goal
            if last_goal is not None:
                return last_goal

    goal = record.get("goal")
    if isinstance(goal, dict):
        return goal
    return None


def goal_objectives_from_record(record: dict[str, Any]) -> list[str]:
    objectives = goal_objectives_from_text(text_from_record(record))
    goal = parsed_goal_from_record(record)
    if isinstance(goal, dict):
        objective = str(goal.get("objective") or "").strip()
        if objective:
            objectives.append(objective)

    command_objective = goal_command_objective_from_text(text_from_record(record))
    if command_objective:
        objectives.append(command_objective)
    return objectives


def goal_completion_index(records: list[dict[str, Any]]) -> int:
    completed_index = -1
    for index, record in enumerate(records):
        goal = parsed_goal_from_record(record)
        if isinstance(goal, dict) and goal.get("status") == "complete":
            completed_index = index
    return completed_index


def goal_auto_review_request_for_completed_goal(payload: dict[str, Any]) -> tuple[bool, str, int]:
    records = transcript_records(payload)
    completed_index = goal_completion_index(records)
    if completed_index < 0:
        return False, "", -1

    for record in records[completed_index + 1 :]:
        if is_user_supplied_record(record):
            return False, "", completed_index

    for record in reversed(records[: completed_index + 1]):
        objectives = goal_objectives_from_record(record)
        if not objectives:
            continue
        objective = objectives[-1]
        return (True, objective, completed_index) if should_arm(objective) else (
            False,
            "",
            completed_index,
        )
    return False, "", completed_index


def latest_goal_auto_review_request(payload: dict[str, Any]) -> tuple[bool, str]:
    records = transcript_records(payload)
    start_index = 0
    completed_index = goal_completion_index(records)
    if completed_index >= 0:
        for index, record in enumerate(records[completed_index + 1 :], start=completed_index + 1):
            if is_user_supplied_record(record):
                start_index = index + 1

    for record in reversed(records[start_index:]):
        objectives = goal_objectives_from_record(record)
        if not objectives:
            continue
        objective = objectives[-1]
        return (True, objective) if should_arm(objective) else (False, "")
    return False, ""


def transcript_has_auto_review_activity(payload: dict[str, Any], start_index: int = 0) -> bool:
    needles = (REVIEW_SENTINEL, FIX_SENTINEL)
    records = transcript_records(payload)
    for record in records[max(0, start_index) :]:
        text = text_from_record(record)
        if any(needle in text for needle in needles):
            return True
    return False


def parse_review_result(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    matches = RESULT_RE.findall(text or "")
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    action = payload.get("action")
    legacy_result = action is None and isinstance(payload.get("issues_found"), bool)
    if legacy_result:
        action = ACTION_FIX if payload["issues_found"] else ACTION_CLEAN
    if action not in {ACTION_CLEAN, ACTION_FIX, ACTION_NEEDS_REPLAN}:
        return None
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return None
    normalized_issues: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            return None
        summary = str(issue.get("summary") or "").strip()
        evidence = str(issue.get("evidence") or "").strip()
        requirement_basis = str(issue.get("requirement_basis") or "").strip()
        minimal_fix = str(issue.get("minimal_fix") or issue.get("fix_hint") or "").strip()
        why_in_scope = str(issue.get("why_in_scope") or "").strip()
        if not summary or not evidence or not minimal_fix:
            return None
        if not legacy_result and (not requirement_basis or not why_in_scope):
            return None
        normalized_issues.append(
            {
                "summary": summary,
                "evidence": evidence,
                "requirement_basis": requirement_basis or "legacy_result",
                "minimal_fix": minimal_fix,
                "why_in_scope": why_in_scope or evidence,
            }
        )

    if action in {ACTION_FIX, ACTION_NEEDS_REPLAN} and not normalized_issues:
        return None
    if action == ACTION_CLEAN and normalized_issues:
        return None
    return action, normalized_issues


def review_prompt(
    review_count: int,
    stage: str,
    target_mode: str,
    previous_issues: list[dict[str, Any]] | None = None,
) -> str:
    issue_shape = (
        '{"summary":"摘要","evidence":"位置、触发、错误行为和影响",'
        '"requirement_basis":"原始需求或修改前合同",'
        '"minimal_fix":"最小修复","why_in_scope":"为何属于本任务"}'
    )
    clean_result = '<auto_review_result>{"action":"clean","issues":[]}</auto_review_result>'
    fix_result = (
        f'<auto_review_result>{{"action":"fix","issues":[{issue_shape}]}}</auto_review_result>'
    )
    replan_result = (
        f'<auto_review_result>{{"action":"needs_replan","issues":[{issue_shape}]}}</auto_review_result>'
    )
    if target_mode == TARGET_EXISTING_CHANGES:
        target_note = (
            "目标是当前未提交修改；优先使用同 session 最近的实质任务作为需求依据。"
            "若找不到原始需求，只审代码可证实的逻辑、回归、兼容性、安全和测试问题，不推测需求遗漏；"
        )
    else:
        target_note = "目标是激活 $auto-review 的任务所产生的修改，以原始请求和修改前仓库合同为依据；"

    summaries = [str(issue.get("summary") or "") for issue in previous_issues or []]
    if stage == REVIEW_STAGE_CLOSURE:
        stage_note = (
            f"当前是定向 closure：验证上一轮问题 {json.dumps(summaries, ensure_ascii=False)}、"
            "本次修复 diff、直接调用方，以及修复是否破坏原始需求或跨边界合同；"
            "不要重新开放对全部累计修改的通用 hunting；"
        )
    else:
        stage_note = (
            "当前是首次 discovery：对目标做一次完整审查，并把同一根因、可由同一修复关闭的受支持变体合并报告；"
        )
    return (
        f"{REVIEW_PROMPT} {REVIEW_SENTINEL} "
        f"第 {review_count} 次 auto-review；{target_note}{stage_note}"
        "本次 diff 新增的文档、schema 和测试属于实现选择，不能自行升级为独立需求依据。"
        "仅报告受支持路径可达、影响实质、有直接证据且修复与风险成比例的问题；"
        "风格偏好、理论加固和缺少依据的合同收紧不阻塞。findings 没有最低数量，不要凑数。"
        "语义分类由你完成：存在范围内最小修复时 action=fix；问题真实但合理解决需要扩大架构、"
        "改变原需求或无法证明有比例合理的最小修复时 action=needs_replan；没有阻塞问题时 action=clean。"
        "选择 fix 前依次比较删除新机制、回退原行为、局部修复和新增架构。"
        f"最后必须输出且仅输出一个机器结果块：clean 用 {clean_result}；fix 用 {fix_result}；"
        f"需要重新规划用 {replan_result}。"
    )


def fix_prompt(issues: list[dict[str, Any]], fix_count: int) -> str:
    issues_json = json.dumps(issues, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{FIX_PROMPT} {FIX_SENTINEL} "
        f"第 {fix_count} 次自动修复。上一轮 review 发现的真实问题 JSON：{issues_json}。"
        "只修复这些已确认问题。按删除新机制、回退原行为、局部修复、新增架构的顺序选择最小方案；"
        "仅处理同一根因且位于原任务范围内的直接等价位置，并补充聚焦的回归测试。"
        "本轮新增的合同不能成为扩大修复范围的理由；若不存在范围内的最小修复，不要引入新 schema、"
        "公共状态机或生命周期，说明原因并结束，下一轮由 model 判定 needs_replan。"
        "结束前自查本轮修复及测试结果，修复由本轮直接引入的具体回归。"
        "完成后正常结束本轮；auto-review 会自动提交下一轮 review prompt。"
        "不要手动提交 review prompt，也不要输出 <auto_review_result>，除非你正在执行 review 阶段。"
    )


def emit_block(prompt: str) -> None:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": prompt,
            },
            ensure_ascii=False,
        )
    )


def issue_fingerprint(issue: dict[str, Any]) -> str:
    identity = {
        "summary": str(issue.get("summary") or "").strip(),
        "evidence": str(issue.get("evidence") or "").strip(),
        "requirement_basis": str(issue.get("requirement_basis") or "").strip(),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def pause_automation(
    payload: dict[str, Any],
    reason: str,
    issues: list[dict[str, Any]],
    source: str,
) -> int:
    summaries = [str(issue.get("summary") or "") for issue in issues]
    append_history(
        {
            "event": "automation_paused",
            "state": state_key(payload),
            "reason": reason,
            "source": source,
            "issue_count": len(issues),
        }
    )
    cleanup_state(payload)
    cleanup_plan_handoff(payload)
    print(
        "<auto_review_paused>"
        + json.dumps(
            {
                "reason": reason,
                "issues": summaries,
                "message": "Automatic repair stopped without marking the review clean.",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "</auto_review_paused>"
    )
    return 0


def finish_needs_replan(
    payload: dict[str, Any],
    issues: list[dict[str, Any]],
    source: str,
) -> int:
    append_history(
        {
            "event": "review_needs_replan",
            "state": state_key(payload),
            "source": source,
            "issue_count": len(issues),
        }
    )
    cleanup_state(payload)
    cleanup_plan_handoff(payload)
    print(
        "<auto_review_needs_replan>"
        "The model found real issues but no proportionate in-scope automatic fix. "
        "Automatic mutation stopped without marking the review clean."
        "</auto_review_needs_replan>"
    )
    return 0


def adopt_plan_handoff(payload: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
    origin_state = str(handoff.get("origin_state_key") or "")
    state = dict(handoff)
    state["phase"] = "deferred_after_plan"
    state["adopted_from_state"] = origin_state
    save_state(payload, state)
    cleanup_plan_handoff(payload, cleanup_origin_state=True)
    if origin_state and origin_state != state_key(payload):
        cleanup_state_key(origin_state, payload)
    append_history(
        {
            "event": "plan_deferred_adopted",
            "state": state_key(payload),
            "origin_state": origin_state,
            "cwd": cwd_from_payload(payload),
        }
    )
    append_debug(
        {
            "event": "plan_deferred_adopted",
            "state": state_key(payload),
            "origin_state": origin_state,
        }
    )
    return state


def record_plan_implementation_prompt(
    payload: dict[str, Any],
    state: dict[str, Any],
    prompt: str,
) -> None:
    state["last_transition"] = "plan_implementation_prompt"
    state["review_target_mode"] = TARGET_TASK_CHANGES
    save_state(payload, state)
    append_history(
        {
            "event": "plan_implementation_prompt",
            "state": state_key(payload),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
    )
    append_debug(
        {
            "event": "plan_implementation_prompt",
            "state": state_key(payload),
            "prompt_prefix": prompt[:160],
        }
    )


def preserve_deferred_plan_for_refinement(
    payload: dict[str, Any],
    state: dict[str, Any],
    prompt: str,
) -> None:
    state["last_transition"] = "plan_refinement_prompt"
    save_state(payload, state)
    save_plan_handoff(payload, state)
    append_history(
        {
            "event": "plan_deferred_refinement",
            "state": state_key(payload),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
    )
    append_debug(
        {
            "event": "plan_deferred_refinement",
            "state": state_key(payload),
            "prompt_prefix": prompt[:160],
        }
    )


def should_keep_deferred_plan_for_prompt(payload: dict[str, Any], prompt: str) -> bool:
    if latest_collaboration_mode(payload) == "plan":
        return True
    return looks_like_plan_refinement_intent(prompt)


def cancel_deferred_plan(
    payload: dict[str, Any],
    prompt: str,
    state: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
) -> None:
    origin_state = ""
    if handoff is not None:
        origin_state = str(handoff.get("origin_state_key") or "")
    append_history(
        {
            "event": "plan_deferred_cancelled",
            "state": origin_state or state_key(payload),
            "current_state": state_key(payload),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
    )
    append_debug(
        {
            "event": "plan_deferred_cancelled",
            "state": origin_state or state_key(payload),
            "current_state": state_key(payload),
            "prompt_prefix": prompt[:160],
        }
    )
    cleanup_state(payload)
    cleanup_plan_handoff(payload, cleanup_origin_state=True)
    if origin_state:
        cleanup_state_key(origin_state, payload)


def handle_user_prompt(payload: dict[str, Any]) -> int:
    prompt = prompt_from_payload(payload)
    if has_auto_review_sentinel(prompt):
        append_debug(
            {
                "event": "user_prompt_ignored_sentinel",
                "state": state_key(payload),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest() if prompt else "",
                "prompt_prefix": prompt[:160],
            }
        )
        return 0

    arm_requested = should_arm(prompt)
    if arm_requested and is_goal_workflow_prompt(prompt):
        state = load_state(payload)
        handoff = load_plan_handoff(payload)
        if state is not None and state.get("phase") == "deferred_after_plan":
            cancel_deferred_plan(payload, prompt, state=state, handoff=handoff)
        elif handoff is not None and payload_owns_plan_handoff(payload, handoff):
            cancel_deferred_plan(payload, prompt, handoff=handoff)
        elif state is not None:
            cleanup_state(payload)
            cleanup_plan_handoff(payload, cleanup_origin_state=True)
        append_debug(
            {
                "event": "goal_prompt_deferred_until_complete",
                "state": state_key(payload),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest() if prompt else "",
                "prompt_prefix": prompt[:160],
            }
        )
        return 0

    if not arm_requested:
        state = load_state(payload)
        if prompt and state is not None and state.get("phase") == "deferred_after_plan":
            if looks_like_plan_implementation_intent(prompt):
                record_plan_implementation_prompt(payload, state, prompt)
                return 0

            if should_keep_deferred_plan_for_prompt(payload, prompt):
                preserve_deferred_plan_for_refinement(payload, state, prompt)
                return 0

            cancel_deferred_plan(payload, prompt, state=state)
            return 0

        if (
            prompt
            and state is not None
            and state.get("phase") == "armed"
            and state.get("last_transition") == "plan_mode_waiting_for_plan_output"
            and looks_like_plan_implementation_intent(prompt)
        ):
            record_plan_implementation_prompt(payload, state, prompt)
            return 0

        if prompt and state is None:
            handoff = load_plan_handoff(payload)
            if handoff is not None:
                if looks_like_plan_implementation_intent(prompt):
                    state = adopt_plan_handoff(payload, handoff)
                    record_plan_implementation_prompt(payload, state, prompt)
                    return 0
                if should_keep_deferred_plan_for_prompt(payload, prompt):
                    append_debug(
                        {
                            "event": "plan_handoff_refinement_prompt_ignored",
                            "state": plan_handoff_origin_state(handoff),
                            "current_state": state_key(payload),
                            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                            "prompt_prefix": prompt[:160],
                        }
                    )
                    return 0
                if payload_owns_plan_handoff(payload, handoff):
                    cancel_deferred_plan(payload, prompt, handoff=handoff)
                    return 0
                append_debug(
                    {
                        "event": "plan_handoff_foreign_prompt_ignored",
                        "state": plan_handoff_origin_state(handoff),
                        "current_state": state_key(payload),
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "prompt_prefix": prompt[:160],
                    }
                )
                return 0

        append_debug(
            {
                "event": "user_prompt_ignored",
                "state": state_key(payload),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest() if prompt else "",
                "prompt_prefix": prompt[:160],
            }
        )
        return 0
    handoff = load_plan_handoff(payload)
    if handoff is not None and payload_owns_plan_handoff(payload, handoff):
        cancel_deferred_plan(payload, prompt, handoff=handoff)
    target_mode, allows_inline = review_target_for_prompt(prompt)
    state = {
        "phase": "armed",
        "review_count": 0,
        "fix_count": 0,
        "review_stage": REVIEW_STAGE_DISCOVERY,
        "activation_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "activation_allows_inline_review_result": allows_inline,
        "review_target_mode": target_mode,
        "seen_issue_fingerprints": [],
    }
    save_state(payload, state)
    append_history({"event": "armed", "state": state_key(payload), "cwd": cwd_from_payload(payload)})
    append_debug({"event": "armed", "state": state_key(payload), "cwd": cwd_from_payload(payload)})
    print(
        "<auto_review_armed>"
        "Stop hook armed for this session. At task Stop, Codex will run one automatic review loop."
        "</auto_review_armed>"
    )
    return 0


def is_subagent_stop(payload: dict[str, Any]) -> bool:
    return payload.get("hook_event_name") == "SubagentStop" or bool(payload.get("parent_session_id"))


def transition_to_review(
    payload: dict[str, Any],
    state: dict[str, Any],
    source: str,
    stage: str | None = None,
) -> int:
    review_count = int(state.get("review_count") or 0) + 1
    review_stage = stage or str(state.get("review_stage") or REVIEW_STAGE_DISCOVERY)
    if source == "plan_implementation_stop":
        state["review_target_mode"] = TARGET_TASK_CHANGES
    state["phase"] = "reviewing"
    state["review_count"] = review_count
    state["review_stage"] = review_stage
    state["last_transition"] = source
    save_state(payload, state)
    if source == "plan_implementation_stop":
        cleanup_plan_handoff(payload)
    append_history(
        {
            "event": "review_prompt",
            "state": state_key(payload),
            "review_count": review_count,
            "review_stage": review_stage,
            "source": source,
        }
    )
    emit_block(
        review_prompt(
            review_count,
            review_stage,
            str(state.get("review_target_mode") or TARGET_TASK_CHANGES),
            state.get("last_issues") if isinstance(state.get("last_issues"), list) else None,
        )
    )
    return 0


def defer_after_plan(payload: dict[str, Any], state: dict[str, Any]) -> int:
    state["phase"] = "deferred_after_plan"
    state["last_transition"] = "plan_stop"
    save_state(payload, state)
    save_plan_handoff(payload, state)
    append_history({"event": "plan_deferred", "state": state_key(payload)})
    append_debug({"event": "plan_deferred", "state": state_key(payload)})
    return 0


def wait_for_plan_output(payload: dict[str, Any], state: dict[str, Any]) -> int:
    state["last_transition"] = "plan_mode_waiting_for_plan_output"
    append_debug({"event": "plan_mode_waiting_for_plan_output", "state": state_key(payload)})
    save_state(payload, state)
    return 0


def plan_implementation_has_started(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    if state.get("last_transition") == "plan_implementation_prompt":
        return True
    if stop_payload_has_plan_implementation_intent(payload):
        return True
    return latest_collaboration_mode(payload) == "default"


def wait_for_plan_implementation(payload: dict[str, Any], state: dict[str, Any]) -> int:
    append_debug({"event": "plan_deferred_waiting_for_implementation", "state": state_key(payload)})
    save_state(payload, state)
    return 0


def transition_goal_completion_to_review(
    payload: dict[str, Any],
    objective: str,
    state: dict[str, Any] | None = None,
) -> int:
    goal_state = dict(state or {})
    goal_state["phase"] = "armed"
    goal_state["review_count"] = 0
    goal_state["fix_count"] = 0
    goal_state["review_stage"] = REVIEW_STAGE_DISCOVERY
    goal_state["last_issues"] = []
    goal_state["activation_prompt_sha256"] = hashlib.sha256(objective.encode()).hexdigest()
    goal_state["activation_source"] = "goal_complete"
    goal_state["review_target_mode"] = TARGET_TASK_CHANGES
    goal_state["seen_issue_fingerprints"] = []
    cleanup_plan_handoff(payload, cleanup_origin_state=True)
    save_state(payload, goal_state)
    append_history(
        {
            "event": "goal_armed_late",
            "state": state_key(payload),
            "cwd": cwd_from_payload(payload),
        }
    )
    append_debug({"event": "goal_armed_late", "state": state_key(payload)})
    return transition_to_review(payload, goal_state, "goal_complete_stop")


def handle_parsed_review_result(
    payload: dict[str, Any],
    state: dict[str, Any],
    action: str,
    issues: list[dict[str, Any]],
    source: str,
) -> int:
    review_stage = str(state.get("review_stage") or REVIEW_STAGE_DISCOVERY)
    if action == ACTION_CLEAN:
        append_history(
            {
                "event": "review_clean",
                "state": state_key(payload),
                "source": source,
                "review_stage": review_stage,
            }
        )
        cleanup_state(payload)
        cleanup_plan_handoff(payload)
        return 0

    if action == ACTION_NEEDS_REPLAN:
        return finish_needs_replan(payload, issues, source)

    seen = {
        str(value)
        for value in state.get("seen_issue_fingerprints", [])
        if isinstance(value, str) and value
    }
    current = {issue_fingerprint(issue) for issue in issues}
    if seen.intersection(current):
        return pause_automation(payload, "repeated_exact_finding", issues, source)

    previous_fix_count = int(state.get("fix_count") or 0)
    if previous_fix_count >= MAX_AUTOMATIC_FIXES:
        return pause_automation(payload, "automatic_fix_budget_exhausted", issues, source)

    fix_count = previous_fix_count + 1
    state["phase"] = "fixing"
    state["fix_count"] = fix_count
    state["last_issues"] = issues
    state["seen_issue_fingerprints"] = sorted(seen.union(current))
    state["last_transition"] = source
    save_state(payload, state)
    append_history(
        {
            "event": "fix_prompt",
            "state": state_key(payload),
            "fix_count": fix_count,
            "issue_count": len(issues),
            "review_stage": review_stage,
            "source": source,
        }
    )
    emit_block(fix_prompt(issues, fix_count))
    return 0


def handle_review_stop(payload: dict[str, Any], state: dict[str, Any]) -> int:
    text = last_assistant_text(payload)
    result = parse_review_result(text)
    if result is None:
        append_history({"event": "review_result_invalid", "state": state_key(payload)})
        print(
            "auto-review: review result marker missing or invalid; automatic repair paused.",
            file=sys.stderr,
        )
        return pause_automation(payload, "review_result_invalid", [], "review_stop")

    action, issues = result
    return handle_parsed_review_result(payload, state, action, issues, "review_stop")


def handle_inline_review_result_if_present(
    payload: dict[str, Any],
    state: dict[str, Any],
    source: str,
) -> int | None:
    if not state.get("activation_allows_inline_review_result"):
        return None

    text = last_assistant_text(payload)
    if "<auto_review_result" not in text:
        return None
    result = parse_review_result(text)
    if result is None:
        return None

    action, issues = result
    append_history(
        {
            "event": "inline_review_result",
            "state": state_key(payload),
            "source": source,
        }
    )
    return handle_parsed_review_result(payload, state, action, issues, source)


def handle_stop(payload: dict[str, Any]) -> int:
    if is_subagent_stop(payload):
        append_debug({"event": "stop_ignored_subagent", "state": state_key(payload)})
        return 0

    state = load_state(payload)
    if state is None:
        requested, objective, completed_index = goal_auto_review_request_for_completed_goal(payload)
        if requested:
            if transcript_has_auto_review_activity(payload, start_index=completed_index + 1):
                append_debug({"event": "goal_complete_already_reviewed", "state": state_key(payload)})
                return 0
            return transition_goal_completion_to_review(payload, objective)
        waiting_for_goal, _ = latest_goal_auto_review_request(payload)
        if waiting_for_goal:
            append_debug({"event": "goal_auto_review_waiting_for_complete", "state": state_key(payload)})
            return 0
        handoff = load_plan_handoff(payload)
        if handoff is not None:
            if not should_adopt_plan_handoff_on_stop(payload, handoff):
                append_debug(
                    {
                        "event": "plan_handoff_foreign_stop_ignored",
                        "state": plan_handoff_origin_state(handoff),
                        "current_state": state_key(payload),
                    }
                )
                return 0
            state = adopt_plan_handoff(payload, handoff)
            return transition_to_review(payload, state, "plan_implementation_stop")
        append_debug({"event": "stop_without_state", "state": state_key(payload)})
        return 0

    phase = state.get("phase")
    if phase == "armed":
        requested, objective, completed_index = goal_auto_review_request_for_completed_goal(payload)
        if requested:
            if transcript_has_auto_review_activity(payload, start_index=completed_index + 1):
                append_debug({"event": "goal_complete_already_reviewed", "state": state_key(payload)})
                cleanup_state(payload)
                return 0
            return transition_goal_completion_to_review(payload, objective, state)
        inline_result = handle_inline_review_result_if_present(payload, state, "armed_inline_review")
        if inline_result is not None:
            return inline_result
        waiting_for_goal, _ = latest_goal_auto_review_request(payload)
        if waiting_for_goal:
            append_debug({"event": "goal_auto_review_waiting_for_complete", "state": state_key(payload)})
            return 0
        if has_plan_output(payload):
            if (
                plan_implementation_has_started(payload, state)
                and latest_collaboration_mode(payload) != "plan"
            ):
                return transition_to_review(payload, state, "plan_implementation_stop")
            return defer_after_plan(payload, state)
        if latest_collaboration_mode(payload) == "plan":
            return wait_for_plan_output(payload, state)
        if state.get("review_target_mode") == TARGET_EXISTING_CHANGES:
            has_changes = git_worktree_has_changes(cwd_from_payload(payload))
            if has_changes is False:
                append_history(
                    {
                        "event": "review_skipped_no_changes",
                        "state": state_key(payload),
                    }
                )
                cleanup_state(payload)
                cleanup_plan_handoff(payload)
                print(
                    "<auto_review_skipped>No uncommitted changes were available to review."
                    "</auto_review_skipped>"
                )
                return 0
        return transition_to_review(payload, state, "task_stop")
    if phase == "deferred_after_plan":
        requested, objective, completed_index = goal_auto_review_request_for_completed_goal(payload)
        if requested:
            if transcript_has_auto_review_activity(payload, start_index=completed_index + 1):
                append_debug({"event": "goal_complete_already_reviewed", "state": state_key(payload)})
                cleanup_state(payload)
                cleanup_plan_handoff(payload)
                return 0
            return transition_goal_completion_to_review(payload, objective, state)
        inline_result = handle_inline_review_result_if_present(
            payload,
            state,
            "deferred_inline_review",
        )
        if inline_result is not None:
            return inline_result
        waiting_for_goal, _ = latest_goal_auto_review_request(payload)
        if waiting_for_goal:
            append_debug({"event": "goal_auto_review_waiting_for_complete", "state": state_key(payload)})
            return 0
        if not plan_implementation_has_started(payload, state):
            return wait_for_plan_implementation(payload, state)
        return transition_to_review(payload, state, "plan_implementation_stop")
    if phase == "reviewing":
        return handle_review_stop(payload, state)
    if phase == "fixing":
        return transition_to_review(
            payload,
            state,
            "fix_stop",
            stage=REVIEW_STAGE_CLOSURE,
        )

    append_history({"event": "state_unknown_phase", "state": state_key(payload), "phase": phase})
    cleanup_state(payload)
    return 0


def main() -> int:
    global _CURRENT_PAYLOAD
    payload = read_stdin_json()
    _CURRENT_PAYLOAD = payload
    event = payload.get("hook_event_name")
    append_debug(
        {
            "event": "hook_invoked",
            "hook_event_name": event,
            "state": state_key(payload),
            "cwd": cwd_from_payload(payload),
            "has_prompt": bool(prompt_from_payload(payload)),
        }
    )
    if event == "UserPromptSubmit":
        return handle_user_prompt(payload)
    if event in {"Stop", "SubagentStop"}:
        return handle_stop(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
