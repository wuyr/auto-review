---
name: auto-review
description: Use when the user wants Codex to automatically run a comprehensive, risk-calibrated review of the current task after completion and, if the review finds real issues, fix them and review again. Trigger with the explicit opt-in prompt "$auto-review".
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

When `/plan` returns a final response containing `<proposed_plan>`, the hook defers review instead of reviewing the plan itself. After the user clicks “实现计划”, clicks “清除当前上下文然后实现计划”, or otherwise asks to implement the plan, the hook runs auto-review when that implementation phase stops. If clearing context creates a new session, the hook can continue the deferred plan through a cwd-scoped handoff. If the original plan session receives an unrelated ordinary prompt while review is deferred after a plan, the hook cancels that deferred state; unrelated prompts in other sessions for the same cwd do not cancel it.

For `/goal` workflows, include `$auto-review` in the goal objective:

```text
/goal $auto-review 完成这个目标，完成后自动 review
```

The hook does not review intermediate goal-continuation turns. When the goal is marked `complete`, the Stop hook reads the goal objective from the transcript and starts the review loop once.

At Stop, the hook will submit this review prompt:

```text
review本次修改，检查是否存在遗漏，逻辑错误等问题
```

If that review reports real issues through the required structured marker, the hook will submit this fix prompt:

```text
修复这些问题然后重新做一次review
```

After the fix pass ends, the hook automatically submits another review prompt. The loop ends when the review result says there are no issues.

## Convergent Review Protocol

Make each automatic review a complete convergence pass. Do not stop after finding the first issue or intentionally save findings for later rounds.

Before producing the result:

1. Reconstruct the requested behavior and inspect the cumulative task changes, not only the latest patch.
2. Derive the important invariants from the request, diff, affected callers, contracts, and tests.
3. Check requirements and logic/data flow; then check sibling call sites, symmetric branches, state combinations, boundaries, and error paths; then check regressions and meaningful test gaps.
4. After finding an issue, search for every supported-path variant of the same root cause. Group variants into one finding when one root-cause fix should close them, and list all affected locations in its evidence.
5. Perform a final sweep before returning the marker. Report every substantiated issue available now; do not cap the list at one or two for brevity.

Measure completeness by review coverage, never by the number of findings. Require no minimum finding count. If the complete pass supports only zero, one, or two real issues, return exactly that number; never invent marginal findings to make the review appear thorough.

On review rounds after a fix, first verify that the previous findings are closed at the root cause, then repeat the complete pass over all cumulative changes. Report a new finding only when the defect still exists or the fix introduced it; do not create findings by progressively tightening an unstated contract.

## Risk Calibration

Mark an item as an issue only when all of these hold:

- Its trigger is reachable through supported usage or an explicitly stated threat model.
- Its impact on correctness, security, data, compatibility, or user-visible behavior is material.
- Code, a deterministic reproduction, a failing test, or an established contract directly supports it.
- The proposed fix is proportionate to the risk and remains within the task's scope.

Do not dismiss a low-frequency path merely because it is rare when the trigger is credible and the impact is high. Conversely, do not require defenses for unsupported inputs, purely theoretical races, arbitrary synchronized tampering with multiple trusted artifacts, or other adversary capabilities outside the stated threat model. Treat style preferences, speculative hardening, broad refactors, and defense-in-depth ideas as non-blocking unless the user explicitly requested them.

For security work, derive the attacker capabilities and trust boundaries from the request and repository contracts. Do not silently strengthen that model during later review rounds.

## Root-Cause Fix Protocol

During an automatic fix pass:

1. Translate each finding into the violated invariant and identify its root cause before editing.
2. Search for and repair affected sibling call sites, symmetric branches, equivalent states, and boundaries instead of patching only the supplied reproduction.
3. Add focused regression coverage for the relevant equivalence classes when practical.
4. Before stopping, self-review the cumulative fix and test results and repair concrete regressions introduced by the fix.
5. Keep the same supported-use and threat-model boundary; do not expand the task into theoretical hardening.

## Review Output Contract

During the automatic review phase, finish with exactly one `auto_review_result` block:

```text
<auto_review_result>
{"issues_found":true,"issues":[{"summary":"问题摘要","evidence":"位置、可达触发、错误行为与实际影响","fix_hint":"修复建议"}]}
</auto_review_result>
```

Use this clean result when no real issue exists:

```text
<auto_review_result>
{"issues_found":false,"issues":[]}
</auto_review_result>
```

Keep each finding concise, but make its evidence state the affected location, reachable trigger, incorrect behavior, and material impact. Only set `issues_found` to `true` for findings that pass the risk-calibration gate above.

## Recursion Guard

Do not invoke `$auto-review` inside the automatic review or fix prompts. Those prompts contain internal sentinels that the hook uses to avoid recursive arming.
