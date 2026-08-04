---
name: auto-review
description: Use when the user wants Codex to automatically run a scope-aware review of task changes or current uncommitted changes, apply proportionate fixes, and stop cleanly or request replanning when automatic repair would expand the task. In the Auto Workflow plugin, trigger with the explicit opt-in prompt "$auto-workflow:auto-review"; the bundled hook also accepts legacy "$auto-review" prompts.
---

# Auto Review

## Workflow

Use `$auto-workflow:auto-review` to opt in when this skill is bundled in the Auto Workflow plugin. The bundled hook also accepts `$auto-review` for existing prompts and prepared workflows. A loaded UserPromptSubmit hook injects an `<auto_review_armed>` acknowledgement. When that acknowledgement is present, let the hook submit review and fix prompts; do not submit them manually.

If the activating turn has no `<auto_review_armed>` acknowledgement, assume the task was created before the plugin was loaded and use this bounded inline fallback instead of silently waiting for a Stop hook:

1. Complete the requested implementation, or inspect the current changes for a standalone review.
2. Run one `discovery` review using the same scope and decision rules below.
3. For `fix`, apply only the enumerated scoped fixes and run `closure`; permit at most two inline fix passes. Stop on a repeated finding, exhausted budget, or `needs_replan`.
4. Prefix the final `auto_review_result` with `<!-- auto-review:inline-fallback -->`. If a hook is actually active, it consumes this result without scheduling a duplicate discovery pass.

For an implementation task, include the marker in the task prompt:

```text
$auto-workflow:auto-review 按这个需求实现...
```

For a standalone review, either form reviews current uncommitted changes:

```text
$auto-workflow:auto-review 检查一下
```

```text
$auto-workflow:auto-review
```

When armed, perform a standalone review in the activating turn and return the structured result directly; the hook consumes it without scheduling a duplicate pass. If no structured result is returned, the Stop hook submits the discovery prompt as a fallback. When unarmed, follow the bounded inline fallback above instead of returning `action=fix` and expecting another turn.

Use the nearest substantive task in the same session as the requirement basis. If it is unavailable, review only code-supported logic, regression, compatibility, security, and test problems; do not infer missing requirements. A clean worktree has no standalone review target, so the hook skips the loop.

For `/plan`, put `$auto-workflow:auto-review` in the initial planning request. The hook defers review when the plan output contains `<proposed_plan>`, then reviews the implementation after the user starts it. A cwd-scoped handoff preserves this intent when implementation starts in a new session. An unrelated prompt in the originating session cancels the deferred state.

For `/goal`, include `$auto-workflow:auto-review` in the objective. Do not review intermediate continuation turns; start after the goal is marked `complete`.

## Review Target and Authority

For implementation prompts, review changes produced by the activating task against the original request and repository contracts that existed before the task. For standalone prompts, review the current uncommitted changes using the nearest recoverable task basis.

Treat documents, schemas, tests, and guarantees added by the reviewed diff as implementation choices. They do not become independent requirements merely because an earlier automatic fix added them. A blocking finding must be supported by an explicit request, a pre-task contract, a regression caused by the task, or a reachable supported-path failure.

## Review Stages

Use an asymmetric convergence flow:

1. `discovery`: inspect the target once and report all substantiated issues available in that pass. Group supported variants when one root-cause fix should close them.
2. `fix`: repair only the reported issues with the smallest proportionate change.
3. `closure`: verify the reported issues, the fix diff, direct callers, original-requirement closure, and directly affected cross-boundary contracts. Do not reopen a general hunt over all cumulative changes.

Treat `action=clean` as terminal in every review stage. The hook must end the loop immediately and must not schedule an additional final review.

The hook permits at most two automatic fix generations. An identical finding after a fix or an exhausted fix budget pauses automation without claiming the review is clean. Python enforces only these deterministic execution guards; the model retains semantic responsibility for deciding whether an issue is real, in scope, fixable, or requires replanning.

## Risk and Decision Calibration

Report a blocking issue only when its supported trigger is reachable, its impact is material, direct evidence supports it, and the proposed response is proportionate to the original task. Do not block on style preferences, speculative hardening, unsupported inputs, purely theoretical races, or contracts invented by the current review loop.

Before choosing a response, compare these options in order:

1. Remove the newly introduced mechanism.
2. Restore the prior supported behavior.
3. Apply a local fix within the task and its direct callers.
4. Add new architecture or broaden the contract.

Use `fix` only when a proportionate in-scope minimum exists. Use `needs_replan` when the issue is real but a reasonable solution requires new architecture, a broader contract, or a material change to the requested design. Use `clean` only when no blocking issue remains.

## Fix Protocol

Fix only the enumerated issues and direct same-root-cause equivalents inside the original scope. Add focused regression coverage when practical. Do not add a schema, public state machine, lifecycle, or broad refactor merely to satisfy a contract introduced by the automatic review itself. If no scoped minimum exists, leave the architecture unchanged so the next review can return `needs_replan`.

## Review Output Contract

Finish every automatic review phase with exactly one `auto_review_result` block.

When clean:

```text
<auto_review_result>
{"action":"clean","issues":[]}
</auto_review_result>
```

When a scoped automatic fix exists:

```text
<auto_review_result>
{"action":"fix","issues":[{"summary":"问题摘要","evidence":"位置、可达触发、错误行为与实际影响","requirement_basis":"原始需求、修改前合同或任务回归","minimal_fix":"范围内最小修复","why_in_scope":"为何属于本任务"}]}
</auto_review_result>
```

When a real issue needs design authority beyond the task:

```text
<auto_review_result>
{"action":"needs_replan","issues":[{"summary":"问题摘要","evidence":"位置、可达触发、错误行为与实际影响","requirement_basis":"原始需求、修改前合同或任务回归","minimal_fix":"说明为何范围内没有合理最小修复","why_in_scope":"为何问题真实且与本任务相关"}]}
</auto_review_result>
```

The hook accepts the legacy `issues_found` result for in-flight compatibility, but new review output must use `action`.

## Recursion Guard

Do not invoke `$auto-workflow:auto-review` or its legacy alias inside automatic review or fix prompts. Internal sentinels prevent recursive arming.
