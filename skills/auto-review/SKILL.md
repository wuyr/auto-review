---
name: auto-review
description: Use when the user wants Codex to automatically review the current task after completion and, if the review finds real issues, automatically fix those issues and run another review. Trigger with the explicit opt-in prompt "$auto-review".
---

# Auto Review

## Workflow

When this skill is used, continue the user's requested coding task normally. The plugin hook arms the session on the activating prompt; do not manually run the review prompt while doing the main task.

Use `$auto-review` to opt in to the automatic review loop.

For ordinary implementation tasks, put `$auto-review` in the task prompt:

```text
$auto-review 按这个需求实现...
```

For `/plan` first workflows, keep the same single entrypoint in the initial planning request:

```text
/plan
$auto-review 为这个需求制定计划：...
```

When `/plan` returns a final response containing `<proposed_plan>`, the hook defers review instead of reviewing the plan itself. After the user clicks “实现计划”, clicks “清除当前上下文然后实现计划”, or otherwise asks to implement the plan, the hook runs auto-review when that implementation phase stops. If clearing context creates a new session, the hook can continue the deferred plan through a cwd-scoped handoff. If the user submits an unrelated ordinary prompt while review is deferred after a plan, the hook cancels the deferred state.

At Stop, the hook will submit this review prompt:

```text
review本次修改，检查是否存在遗漏，逻辑错误等问题
```

If that review reports real issues through the required structured marker, the hook will submit this fix prompt:

```text
修复这些问题然后重新做一次review
```

After the fix pass ends, the hook automatically submits another review prompt. The loop ends when the review result says there are no issues.

## Review Output Contract

During the automatic review phase, finish with exactly one `auto_review_result` block:

```text
<auto_review_result>
{"issues_found":true,"issues":[{"summary":"问题摘要","evidence":"文件/行为证据","fix_hint":"修复建议"}]}
</auto_review_result>
```

Use this clean result when no real issue exists:

```text
<auto_review_result>
{"issues_found":false,"issues":[]}
</auto_review_result>
```

Only set `issues_found` to `true` for concrete missed requirements, logic errors, regressions, or meaningful test gaps. Do not mark style preferences, unverified guesses, or unrelated improvements as issues.

## Recursion Guard

Do not invoke `$auto-review` inside the automatic review or fix prompts. Those prompts contain internal sentinels that the hook uses to avoid recursive arming.
