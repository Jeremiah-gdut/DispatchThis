# Use function phase state for workflow coordination

DispatchThis will coordinate per-function workflow passes through a function phase state module backed by `Function.session_data`.

The initial scope is indirect branch resolving and indirect call resolving. Deflattening and cleanup will join this phase model only after their ARM64-specific behavior is corrected.

The first implementation will not migrate deflattening or cleanup state. It will cover indirect branch resolving receipts/stability, indirect call resolving receipts/stability, and the branch condition translation gate.

Each workflow phase records both readiness and the reanalysis-triggering mutations it has already submitted. Indirect branch resolving records branch sources whose user branch metadata has been applied. Indirect call resolving separately records verified call destinations and completed call-type decisions: either a concrete override was read back, or current evidence required no override. Later phases use these records to skip old mutations and only submit new facts.

Indirect branch resolving and indirect call resolving are split into two layers:

- a pure resolution plan that can be rebuilt every workflow run
- a decision/receipt layer that decides whether to submit Binary Ninja function-state edits

Indirect call resolving depends on indirect branch resolving being stable. Branch condition translation waits for branch, call, and global constant resolving to be stable; its workflow activity follows global resolving so data-var edits and their reanalysis settle before the expensive CFG overlay is installed. Translation is a presentation rewrite over the current MLIL and does not own mutation receipts.

Phase cleanup runs only after its owning phase is stable. For indirect branch resolving, cleanup runs after branch condition translation so the translator can still read the resolved `MLIL_JUMP_TO` shape and the target-decode assignments it needs. For indirect call resolving, cleanup runs after indirect call resolving is stable.

Indirect branch and indirect call phase cleanup will reuse the existing decode-gadget taint/dead-residue ideas, but not the full deflatten cleanup pass. Phase cleanup may NOP pure decode computations; call cleanup may also NOP an explicitly plan-owned, SSA-dead load in the complete `call.dest` definition slice. It must not collapse control flow, trust xrefs as ownership evidence, or NOP deflatten state writes.

Phase cleanup must be rooted in the owning phase's resolved sites rather than in all decode-gadget magic constants. Indirect branch cleanup roots from resolved branch-target decode sites. Indirect call cleanup roots from resolved call-target decode sites. This prevents branch cleanup from deleting call-target decode inputs before indirect call resolving has run.

Cleanup receipts are phase-level booleans on the function, not per branch source or per call site. A phase cleanup scans the current function MLIL for its owning phase and sets `cleanup_done` only when that scan makes no IL changes. If cleanup does NOP dead decode instructions, the receipt stays open so a later workflow run can confirm the overlay survived Binary Ninja reanalysis. Upstream receipt changes also invalidate the owning cleanup receipt.

Workflow callbacks are the orchestration seam. Pass modules may produce plans and perform current-IL rewrites, but workflow callbacks own Binary Ninja reanalysis-triggering mutations such as user branch metadata, call type adjustments, and analysis completion callbacks.

The function phase state module exposes phase semantic operations rather than raw dictionaries or sets. Moving raw dict access from `BinaryView.session_data` to `Function.session_data` is not enough; the module owns readiness, receipt comparison, and downstream invalidation rules.

Indirect branch resolving is stable only when all current unresolved indirect branch sources are covered by user branch metadata, no new branch mutation was submitted in the current run, and no receipt target changed. If a source's resolved targets differ from its receipt, DispatchThis treats that as downstream invalidation: it logs the change, updates the receipt, resubmits user branch metadata, and clears dependent indirect call resolving receipts.

When function phase state is empty but Binary Ninja already has user indirect branch metadata for the function, DispatchThis seeds branch receipts from that metadata. This covers plugin hot reloads and reopened BNDBs without resubmitting the same branch mutations.

Branch receipts may narrow the next recognition run only after read-back. The
workflow compares each receipt's complete normalized target tuple with Binary
Ninja's current non-auto user branch metadata and passes only exact matches as
the verified branch frontier. A receipt with missing, automatic, subset,
superset, or changed metadata remains in the recognition frontier. This keeps
incremental convergence from repeatedly decoding BN's resolved `LLIL_JUMP_TO`
shape without allowing session state by itself to prune a current branch.

This is necessary because Binary Ninja function-state edits can schedule function analysis again and re-enter the workflow. A boolean stable flag alone is not enough: repeating the same mutation can keep analysis running even when the recovered facts have not changed.

`Unresolved Indirect Control Flow` tag cleanup is not part of branch resolving stability. Once branch resolving is stable, DispatchThis schedules an analysis completion callback to remove those tags from sources covered by user branch metadata.

View-level state remains valid only for BinaryView-scoped timing, such as tag cleanup pending state tied to `BinaryView.add_analysis_completion_event()`.

Function phase state is bound to the active resolver profile ID. Empty state may
be rebound, but legacy or mismatched state that already contains recovery
evidence fails closed; receipts from one binary profile are not evidence under
another profile.

Receipts coordinate submissions but do not replace Binary Ninja's current
facts. Branch stability reads back user branch metadata, concrete call type
overrides are verified with `get_call_type_adjustment`, and global stability
reads back the current data-variable type. A completed no-override call decision
does not submit or compare `None` against BN's effective automatic type.
Current-IL rewrites additionally require the exact instruction witness described
by ADR-0011.
