# Source layout

```
DispatchThis/
├── __init__.py                 Plugin entry point: registers the workflow + activities,
│                               the settings, the analysis-limit overrides, and the
│                               Plugins-menu Enable/Disable toggles.
├── workflow.py                 The workflow activity callbacks (LLIL jump resolve,
│                               MLIL call/global resolve, deflatten, cleanup) and their gating.
├── utils/
│   └── log.py                  Shared "DispatchThis" logger.
├── passes/
│   ├── low/
│   │   └── gadget_llil.py      LLIL decode-gadget resolver: jump(reg) -> jump(const),
│   │                           including opaque-predicate offset selection.
│   └── medium/
│       ├── indirect_calls.py   MLIL indirect-call decode fold + call-type adjustment.
│       ├── global_constants.py MLIL global constant slot planner.
│       ├── phase_cleanup.py    One-shot branch/call target-decode cleanup.
│       ├── deflatten.py        Computes and applies dispatcher state-token redirections.
│       ├── nop_pass.py         Deflatten state-write NOPing.
│       └── REFERENCE_conditional_obb.md   Annotated reference example for the
│                                          conditional transition handling.
├── assets/                     README screenshots.
├── docs/                       This documentation.
├── README.md
└── LICENSE
```

## Module responsibilities

### `__init__.py`
Clones `core.function.metaAnalysis`, registers the activities and their insertion
points, registers the boolean setting (`dispatchthis.enableDeflatten`, raises analysis limits, and wires up the `Enable`/`Disable` menu pair for each pass.

### `workflow.py`
The activity callbacks invoked by the workflow per function. Each reads the relevant IL
off the `AnalysisContext`, calls into a pass module, and owns reanalysis-triggering Binary
Ninja edits plus the phase/session receipts that gate them.

### `passes/low/gadget_llil.py`
Parses the three-step decode gadget backwards (`parse_jump_gadget`), recovers
`(slot, displacement, key, offset)`, decodes the jump target via a per-function key, and
returns a branch plan. It may rewrite the current LLIL, but workflow owns user branch
metadata and analysis-completion callback scheduling.

### `passes/medium/indirect_calls.py`
Builds call-target plans, folds call-gadget decode expressions, rewrites the current MLIL
call destination to a const pointer, and returns cleanup roots. Workflow owns call type
adjustments and receipts.

### `passes/medium/global_constants.py`
Finds `.data` qword slots that are used as read-only constant pointer bases and returns
type-mutation plans for the workflow callback.

### `passes/medium/deflatten.py`
`compute_redirections` identifies the dominant dispatcher comparison cluster, maps state
tokens to target blocks, and returns terminator re-pointings; `apply_redirections_il`
rewrites the terminators. Handles unconditional and simple conditional transitions; see
[`conditional-deflattening.md`](conditional-deflattening.md).

### `passes/medium/nop_pass.py`
`nop_deflatten_state_writes` runs after the deflattener and NOPs dispatcher state writes
using the state tokens and variables recorded by the workflow.
