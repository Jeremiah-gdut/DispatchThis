# Source layout

```
DispatchThis/
├── __init__.py                 Plugin entry point: registers the workflow + activities,
│                               the settings, the analysis-limit overrides, and the
│                               Function Analysis setting activities.
├── workflow.py                 The workflow activity callbacks (LLIL jump resolve,
│                               MLIL call/global resolve, branch translation,
│                               string decrypt, deflatten, phase cleanup,
│                               and deflatten cleanup) and their gating.
├── workflow_state.py           Function-scoped workflow phase receipts and stability.
├── ui.py                       Function context-menu commands and shortcuts for
│                               selecting profiles and toggling workflow settings.
├── profiles/
│   ├── __init__.py             Bundled resolver profile registry and contract validation.
│   ├── default.py              Built-in resolver profile for the current binary.
│   └── dyzznb.py               Bundled resolver profile for the dyzznb sample.
├── helpers/
│   ├── __init__.py             Stable profile-helper import surface.
│   ├── llil.py                 LLIL indirect-jump, definition, and constant helpers.
│   ├── mlil.py                 MLIL call-target, slot, store, deflatten-planner,
│   │                           and cleanup-root helpers.
│   ├── memory.py               BinaryView memory, section, and target validation helpers.
│   └── facts.py                Recovery fact builders for resolver profiles and passes.
├── utils/
│   └── log.py                  Shared "DispatchThis" logger.
├── passes/
│   ├── low/
│   │   └── gadget_llil.py      LLIL decode-gadget resolver: jump(reg) -> jump(const),
│   │                           including opaque-predicate offset selection.
│   └── medium/
│       ├── indirect_calls.py   MLIL indirect-call decode fold and current-IL rewrites.
│       ├── global_constants.py MLIL global constant slot planner.
│       ├── string_decrypt.py   MLIL direct-call string decrypt recognizer/commenter.
│       ├── phase_cleanup.py    One-shot branch/call target-decode cleanup.
│       ├── rewrite.py          Atomic MLIL copy-transform backend for control-flow rewrites.
│       ├── deflatten.py        Computes dispatcher plans and builds replacement MLIL.
│       └── nop_pass.py         Deflatten state-write NOPing.
├── docs/                       This documentation.
│   ├── API.md                  Helper API reference for resolver profiles.
│   ├── conditional-deflattening.md
│   ├── files.md                This source map.
│   ├── known-issues.md
│   ├── obfuscation.md
│   ├── pipeline.md
│   ├── resolver-profiles.md    How to add bundled binary resolver profiles.
│   ├── adr/                    Architecture decision records.
│   └── agents/                 Agent workflow notes.
├── README.md
└── LICENSE
```

## Module responsibilities

### `__init__.py`
Clones `core.function.metaAnalysis`, registers the activities and their insertion
points, surfaces the `analysis.plugins.dispatchThis.indirectJumpsCalls`,
`analysis.plugins.dispatchThis.stringDecrypt`, and `analysis.plugins.dispatchThis.deflatten`
Function Analysis settings, and raises analysis limits for large flattened functions.

### `workflow.py`
The activity callbacks invoked by the workflow per function. Each reads the relevant IL
off the `AnalysisContext`, calls into a pass module, and owns reanalysis-triggering Binary
Ninja edits plus the phase/session receipts that gate them.

### `workflow_state.py`
Owns `Function.session_data["dispatchthis_workflow_state"]`: indirect branch, indirect
call, and global constant workflow phase stability, mutation receipts, and downstream invalidation. See
[`adr/0003-function-phase-state-for-workflow.md`](adr/0003-function-phase-state-for-workflow.md).

### `profiles/`
Owns the bundled resolver profile registry. The built-in `default` profile exposes
the current indirect branch, indirect call, global constant, and string decrypt
resolver behavior plus the default deflatten redirection planner behind the
resolver profile contract. See
[`resolver-profiles.md`](resolver-profiles.md) before adding a new binary
profile.

### `helpers/`
Stable profile-helper modules for reusable BNIL and BinaryView inspection:
`llil`, `mlil`, `memory`, and `facts`. Helpers reduce repeated profile code, but
profiles still own binary-specific recognition; the recovery backend owns Binary
Ninja mutations, phase receipts, IL rewrites, and cleanup application.

### `passes/low/gadget_llil.py`
Parses decode-gadget `jump(reg)` and tail-call forms, recovers table slots, table-base
keys, decode keys, and entry offsets, then returns a branch plan. It may rewrite the
current LLIL, but workflow owns user branch metadata and analysis-completion callback
scheduling.

### `passes/medium/indirect_calls.py`
Builds call-target plans, folds call-gadget decode expressions, rewrites the current MLIL
call destination to a const pointer, and returns cleanup roots. Workflow owns call type
adjustments, receipts, and call-target phase cleanup.

### `passes/medium/global_constants.py`
Finds `.data` qword slots that are used as read-only constant pointer bases and returns
type-mutation plans for the workflow callback.

### `passes/medium/string_decrypt.py`
Scans the current function's MLIL direct calls, recognizes calls to deflattened
sample-family string decrypt functions, decodes the source blob, and writes function-level
call-site comments while preserving manual comment lines.

### `passes/medium/phase_cleanup.py`
Runs branch-target and call-target phase cleanup. It NOPs dead pure target-decode
assignments rooted at the owning workflow phase's resolved sites; it does not collapse
control flow or remove deflatten state writes.

### `passes/medium/deflatten.py`
`compute_redirections` identifies the dominant dispatcher comparison cluster, maps state
tokens to target blocks, and returns terminator re-pointings;
`rewrite_redirections_mlil` turns all selected plans into one atomic replacement MLIL
function. The workflow installs it before publishing deflatten state maps. Handles
unconditional and simple conditional transitions; see
[`conditional-deflattening.md`](conditional-deflattening.md).

### `passes/medium/nop_pass.py`
`nop_deflatten_state_writes` runs after the deflattener and NOPs dispatcher state writes
using the state tokens and variables recorded by the workflow.
