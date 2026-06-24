# Source layout

```
DispatchThis/
├── __init__.py                 Plugin entry point: registers the workflow + activities,
│                               the settings, the analysis-limit overrides, and the
│                               Plugins-menu Enable/Disable toggles.
├── workflow.py                 The four workflow activity callbacks (LLIL jump resolve,
│                               MLIL call resolve, deflatten, cleanup) and their gating.
├── utils/
│   ├── log.py                  Shared "DispatchThis" logger.
│   └── state_machine.py        StateMachine: recovers the state variable, the backbone
│                               map {state -> comparator block}, and OBB -> successor links.
├── passes/
│   ├── low/
│   │   └── gadget_llil.py      LLIL decode-gadget resolver: jump(reg) -> jump(const),
│   │                           including opaque-predicate offset selection.
│   └── medium/
│       ├── indirect_calls.py   MLIL indirect-call decode fold + call-type adjustment.
│       ├── deflatten.py        Computes and applies the OBB -> goto redirections,
│       │                       including the conditional/Z3 path.
│       ├── nop_pass.py         Signature-based gadget cleanup, dead-decode residue
│       │                       removal, and precise state-write NOPing.
│       └── REFERENCE_conditional_obb.md   Annotated reference example for the
│                                          conditional transition handling.
├── assets/                     README screenshots.
├── docs/                       This documentation.
├── README.md
└── LICENSE
```

## Module responsibilities

### `__init__.py`
Clones `core.function.metaAnalysis`, registers the four activities and their insertion
points, registers the boolean setting (`dispatchthis.enableDeflatten`, raises analysis limits, and wires up the `Enable`/`Disable` menu pair for each pass.

### `workflow.py`
The activity callbacks invoked by the workflow per function. Each is thin: it reads the
relevant IL off the `AnalysisContext`, calls into a pass module, and manages the
`session_data` gating (LLIL stability, MLIL stability, recorded state constants/vars).

### `utils/state_machine.py`
Read-only analysis. `StateMachine.analyze()` finds the state variable (the variable in the
most equality compares), builds the backbone from its constant compares, enumerates every
state write (direct and through aliases / pointer stores), and resolves each write to the
real successor(s) via `match_successor`. Produces `CFGLink`s the deflattener consumes.

### `passes/low/gadget_llil.py`
Parses the three-step decode gadget backwards (`parse_jump_gadget`), recovers
`(slot, displacement, key, offset)`, decodes the jump target via a per-function key, and
rewrites the jump. Includes the opaque-predicate evaluator and the phi/VSA constant
recovery for the displacement/key registers the dispatcher merges.

### `passes/medium/indirect_calls.py`
Folds the call-gadget decode (`eval_const`), rewrites the call destination to a const
pointer, folds the spilled decode definition, and pins the callee prototype once per call
site per session.

### `passes/medium/deflatten.py`
`compute_redirections` turns the state-machine links + resolved gadget map into a set of
jump re-pointings; `apply_redirections_il` rewrites the terminators. Handles both
unconditional and conditional (cmov-selected) transitions; see
[`conditional-deflattening.md`](conditional-deflattening.md).

### `passes/medium/nop_pass.py`
`clean_resolved_gadget_jumps` runs after the deflattener: converts remaining single-target
jumps to gotos, collapses always-true opaque predicates, and NOPs the gadget taint set,
dead decode residue, and state writes - all to a fixpoint, pure IL only.
