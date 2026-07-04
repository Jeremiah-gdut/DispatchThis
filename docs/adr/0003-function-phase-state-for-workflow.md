# Use function phase state for workflow coordination

DispatchThis will coordinate per-function workflow passes through a function phase state module backed by `Function.session_data`.

The initial scope is indirect branch resolving and indirect call resolving. Deflattening and cleanup will join this phase model only after their ARM64-specific behavior is corrected.

The first implementation will not migrate deflattening or cleanup state. It will cover indirect branch resolving receipts/stability, indirect call resolving receipts/stability, and the branch condition translation gate.

Each workflow phase records both readiness and the reanalysis-triggering mutations it has already submitted. Indirect branch resolving records branch sources whose user branch metadata has been applied. Indirect call resolving records call sites whose call type adjustments have been applied. Later phases use these records to skip old mutations and only submit new facts.

Indirect branch resolving and indirect call resolving are split into two layers:

- a pure resolution plan that can be rebuilt every workflow run
- a mutation receipt layer that decides whether to submit Binary Ninja function-state edits

Indirect call resolving depends on indirect branch resolving being stable. Branch condition translation is a presentation rewrite over the current MLIL and does not own mutation receipts.

Workflow callbacks are the orchestration seam. Pass modules may produce plans and perform current-IL rewrites, but workflow callbacks own Binary Ninja reanalysis-triggering mutations such as user branch metadata, call type adjustments, and analysis completion callbacks.

The function phase state module exposes phase semantic operations rather than raw dictionaries or sets. Moving raw dict access from `BinaryView.session_data` to `Function.session_data` is not enough; the module owns readiness, receipt comparison, and downstream invalidation rules.

Indirect branch resolving is stable only when all current unresolved indirect branch sources are covered by user branch metadata, no new branch mutation was submitted in the current run, and no receipt target changed. If a source's resolved targets differ from its receipt, DispatchThis treats that as downstream invalidation: it logs the change, updates the receipt, resubmits user branch metadata, and clears dependent indirect call resolving receipts.

When function phase state is empty but Binary Ninja already has user indirect branch metadata for the function, DispatchThis seeds branch receipts from that metadata. This covers plugin hot reloads and reopened BNDBs without resubmitting the same branch mutations.

This is necessary because Binary Ninja function-state edits can schedule function analysis again and re-enter the workflow. A boolean stable flag alone is not enough: repeating the same mutation can keep analysis running even when the recovered facts have not changed.

`Unresolved Indirect Control Flow` tag cleanup is not part of branch resolving stability. Once branch resolving is stable, DispatchThis schedules an analysis completion callback to remove those tags from sources covered by user branch metadata.

View-level state remains valid only for BinaryView-scoped timing, such as tag cleanup pending state tied to `BinaryView.add_analysis_completion_event()`.
