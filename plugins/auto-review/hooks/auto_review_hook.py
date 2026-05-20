#!/usr/bin/env python3
"""Codex hook entrypoint for the auto-review plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
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
STATE_TTL_SECONDS = 12 * 60 * 60


def state_base() -> Path:
    override = os.environ.get("AUTO_REVIEW_STATE_HOME") or os.environ.get(
        "AUTO_REVIEW_LOOP_STATE_HOME"
    )
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "auto-review"


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


def state_path_for_key(key: str) -> Path:
    return state_base() / "state" / f"{key}.json"


def state_path(payload: dict[str, Any]) -> Path:
    return state_path_for_key(state_key(payload))


def plan_handoff_path(payload: dict[str, Any]) -> Path:
    return state_base() / "deferred-plan" / f"{cwd_key(payload)}.json"


def history_path() -> Path:
    return state_base() / "history.jsonl"


def debug_path() -> Path:
    return state_base() / "debug.jsonl"


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


def cleanup_state(payload: dict[str, Any]) -> None:
    cleanup_state_key(state_key(payload))


def cleanup_state_key(key: str) -> None:
    if not key:
        return
    try:
        state_path_for_key(key).unlink()
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
        cleanup_state_key(origin_state)


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


def has_auto_review_sentinel(prompt: str) -> bool:
    return REVIEW_SENTINEL in prompt or FIX_SENTINEL in prompt


def is_proposed_plan_output(text: str) -> bool:
    return bool(PROPOSED_PLAN_RE.search(text or ""))


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


def assistant_text_from_record(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return text_from_value(message.get("content"))

    payload = record.get("payload")
    if isinstance(payload, dict):
        payload_message = payload.get("message")
        if isinstance(payload_message, dict) and payload_message.get("role") == "assistant":
            return text_from_value(payload_message.get("content"))
        if payload.get("role") == "assistant":
            return text_from_value(payload.get("content"))

    if record.get("role") == "assistant":
        return text_from_value(record.get("content"))
    return ""


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


def parse_review_result(text: str) -> tuple[bool, list[dict[str, Any]]] | None:
    matches = RESULT_RE.findall(text or "")
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    issues_found = payload.get("issues_found")
    issues = payload.get("issues")
    if not isinstance(issues_found, bool) or not isinstance(issues, list):
        return None
    normalized_issues: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            return None
        summary = str(issue.get("summary") or "").strip()
        evidence = str(issue.get("evidence") or "").strip()
        fix_hint = str(issue.get("fix_hint") or "").strip()
        if not summary:
            return None
        normalized_issues.append(
            {
                "summary": summary,
                "evidence": evidence,
                "fix_hint": fix_hint,
            }
        )

    if issues_found and not normalized_issues:
        return None
    if not issues_found and normalized_issues:
        return None
    return issues_found, normalized_issues


def review_prompt(review_count: int) -> str:
    issue_result = (
        '<auto_review_result>{"issues_found":true,"issues":[{"summary":"问题摘要",'
        '"evidence":"文件/行为证据","fix_hint":"修复建议"}]}</auto_review_result>'
    )
    clean_result = '<auto_review_result>{"issues_found":false,"issues":[]}</auto_review_result>'
    return (
        f"{REVIEW_PROMPT} {REVIEW_SENTINEL} "
        f"第 {review_count} 次 auto-review：只审本次任务修改，重点查遗漏、逻辑错误、回归风险、测试缺口；"
        "发现真实问题时先简洁列出问题和证据，无问题则说明未发现阻塞问题；"
        f"最后必须输出机器可解析结果块：有问题用 {issue_result}，无问题用 {clean_result}；"
        "不要把格式问题、风格偏好或未证实猜测标记为 issues_found=true。"
    )


def fix_prompt(issues: list[dict[str, Any]], fix_count: int) -> str:
    issues_json = json.dumps(issues, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{FIX_PROMPT} {FIX_SENTINEL} "
        f"第 {fix_count} 次自动修复。上一轮 review 发现的真实问题 JSON：{issues_json}。"
        "请修复这些问题，完成后正常结束本轮；auto-review 会自动提交下一轮 review prompt。"
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


def adopt_plan_handoff(payload: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
    origin_state = str(handoff.get("origin_state_key") or "")
    state = dict(handoff)
    state["phase"] = "deferred_after_plan"
    state["adopted_from_state"] = origin_state
    save_state(payload, state)
    cleanup_plan_handoff(payload)
    if origin_state and origin_state != state_key(payload):
        cleanup_state_key(origin_state)
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
        cleanup_state_key(origin_state)


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

    if not should_arm(prompt):
        state = load_state(payload)
        if prompt and state is not None and state.get("phase") == "deferred_after_plan":
            if looks_like_plan_implementation_intent(prompt):
                record_plan_implementation_prompt(payload, state, prompt)
                return 0

            cancel_deferred_plan(payload, prompt, state=state)
            return 0

        if prompt and state is None:
            handoff = load_plan_handoff(payload)
            if handoff is not None:
                if looks_like_plan_implementation_intent(prompt):
                    state = adopt_plan_handoff(payload, handoff)
                    record_plan_implementation_prompt(payload, state, prompt)
                    return 0
                cancel_deferred_plan(payload, prompt, handoff=handoff)
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
    if handoff is not None:
        cancel_deferred_plan(payload, prompt, handoff=handoff)
    state = {
        "phase": "armed",
        "review_count": 0,
        "fix_count": 0,
        "activation_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
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


def transition_to_review(payload: dict[str, Any], state: dict[str, Any], source: str) -> int:
    review_count = int(state.get("review_count") or 0) + 1
    state["phase"] = "reviewing"
    state["review_count"] = review_count
    state["last_transition"] = source
    save_state(payload, state)
    if source == "plan_implementation_stop":
        cleanup_plan_handoff(payload)
    append_history(
        {
            "event": "review_prompt",
            "state": state_key(payload),
            "review_count": review_count,
            "source": source,
        }
    )
    emit_block(review_prompt(review_count))
    return 0


def defer_after_plan(payload: dict[str, Any], state: dict[str, Any]) -> int:
    state["phase"] = "deferred_after_plan"
    state["last_transition"] = "plan_stop"
    save_state(payload, state)
    save_plan_handoff(payload, state)
    append_history({"event": "plan_deferred", "state": state_key(payload)})
    append_debug({"event": "plan_deferred", "state": state_key(payload)})
    return 0


def handle_review_stop(payload: dict[str, Any], state: dict[str, Any]) -> int:
    text = last_assistant_text(payload)
    result = parse_review_result(text)
    if result is None:
        append_history({"event": "review_result_invalid", "state": state_key(payload)})
        cleanup_state(payload)
        cleanup_plan_handoff(payload)
        print(
            "auto-review: review result marker missing or invalid; loop stopped without guessing.",
            file=sys.stderr,
        )
        return 0

    issues_found, issues = result
    if not issues_found:
        append_history({"event": "review_clean", "state": state_key(payload)})
        cleanup_state(payload)
        cleanup_plan_handoff(payload)
        return 0

    fix_count = int(state.get("fix_count") or 0) + 1
    state["phase"] = "fixing"
    state["fix_count"] = fix_count
    state["last_issues"] = issues
    save_state(payload, state)
    append_history(
        {
            "event": "fix_prompt",
            "state": state_key(payload),
            "fix_count": fix_count,
            "issue_count": len(issues),
        }
    )
    emit_block(fix_prompt(issues, fix_count))
    return 0


def handle_stop(payload: dict[str, Any]) -> int:
    if is_subagent_stop(payload):
        append_debug({"event": "stop_ignored_subagent", "state": state_key(payload)})
        return 0

    state = load_state(payload)
    if state is None:
        handoff = load_plan_handoff(payload)
        if handoff is not None:
            state = adopt_plan_handoff(payload, handoff)
            return transition_to_review(payload, state, "plan_implementation_stop")
        append_debug({"event": "stop_without_state", "state": state_key(payload)})
        return 0

    phase = state.get("phase")
    if phase == "armed":
        if is_proposed_plan_output(last_assistant_text(payload)):
            return defer_after_plan(payload, state)
        return transition_to_review(payload, state, "task_stop")
    if phase == "deferred_after_plan":
        return transition_to_review(payload, state, "plan_implementation_stop")
    if phase == "reviewing":
        return handle_review_stop(payload, state)
    if phase == "fixing":
        return transition_to_review(payload, state, "fix_stop")

    append_history({"event": "state_unknown_phase", "state": state_key(payload), "phase": phase})
    cleanup_state(payload)
    return 0


def main() -> int:
    payload = read_stdin_json()
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
