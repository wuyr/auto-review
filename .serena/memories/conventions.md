# Conventions
- Preserve user-owned dirty changes; this repository is commonly edited as a coordinated plugin/skill/docs update.
- Runtime decisions are deterministic Python state transitions; semantic review judgments remain model-owned through the structured `auto_review_result` contract.
- `action=clean` is terminal in every review stage: clean must remove session state and emit no follow-up blocking review.
- Hook output blocks use compact JSON and must remain machine-parseable; Stop-hook blocking is represented by emitted decision JSON.
- Keep legacy structured-result parsing for in-flight compatibility when changing the current result schema.
- Behavior contract changes should update runtime, focused regression tests, bundled skill text, and both README languages together.
- Use atomic state writes and fail-open cleanup patterns already present in the hook.
- Local plugin development updates use the plugin-creator cachebuster/reinstall flow; never hand-edit marketplace config for cache invalidation.