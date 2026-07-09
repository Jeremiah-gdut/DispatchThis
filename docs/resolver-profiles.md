# Resolver profiles

Resolver profiles adapt DispatchThis to one binary without moving workflow
ownership into profile-specific code.

A profile is selected per BinaryView with
`analysis.plugins.dispatchThis.resolverProfile`. Function workflow enablement is
still per function; selecting a profile does not enable DispatchThis for every
function in the view.

## Agent workflow

Do not start by editing code. First collect the binary facts below. If a field is
not used by the binary, write `none`.

```text
binary:
  file:
  architecture:
  platform:
  profile_id:
  profile_name:

capabilities:
  branch_gadget:
  call_gadget:
  global_constants:
  deflatten:
  string_decrypt:

branch_gadget:
  unresolved_jump_addr:
  llil_excerpt:
  notes:
  decoded_targets:

call_gadget:
  indirect_call_addr:
  mlil_excerpt:
  notes:
  decoded_callee:
  cleanup_roots:

global_constants:
  slot_addr:
  mlil_excerpt:
  notes:
  resolved_addr:

deflatten:
  dispatcher_state:
  mlil_excerpt:
  notes:
  redirection_count:

string_decrypt:
  call_addr:
  callee_addr:
  source_blob_addr:
  expected_plaintext:

validation:
  pytest:
  bn_commands:
  manual_bndb_checks:
```

Then implement the smallest profile that satisfies those facts:

1. Add `plugins/DispatchThis/profiles/<profile_id>.py`.
2. Define the metadata and every required hook.
3. Return `[]` from hooks the binary does not need.
4. Register the module in `plugins/DispatchThis/profiles/__init__.py`.
5. Complete the definition of done below.

`llil_excerpt` and `mlil_excerpt` must be raw Binary Ninja IL copied from the
target. Notes may explain the interpretation, but they do not replace the raw
IL. Keep excerpts short, but include the addresses, instruction indices,
variables, and key expressions needed to reproduce the shape.

Keep binary-specific matching in the profile file until two profiles need the
same helper or the profile becomes hard to read. Shared profile code must move
to stable helper modules under `helpers/`, or a profile must explicitly delegate
to another named profile. Do not add `profiles/_shared.py`,
`profiles/<family>_shared.py`, or any resolver engine/DSL/base class.
Shared `passes/` code is for stable workflow-level capabilities, not for one
binary or speculative reuse.

## Naming

Create one profile per binary by default. `PROFILE_ID` must be stable lowercase
snake_case, such as `dy_libdyzznb_202607` or `dyzznb_main`.

Do not use vague names such as `sample1`, `new_profile`, `current`, or
`default2`. Do not include full local paths, usernames, customer names, or other
sensitive project labels. `PROFILE_NAME` can be human-readable, and
`PROFILE_DESCRIPTION` should state the binary identity and supported capabilities.

## Sensitive information

Profile code, metadata, tests, comments, and capability matrices must not include
local absolute paths, usernames, customer names, private sample sources, or other
sensitive project labels. If traceability is needed, use a file basename, date,
hash prefix, or another non-sensitive identifier.

## Capability matrix

Every binary profile must declare which required hooks are real and which are
intentional no-ops. Keep this near the top of the profile module as a comment:

```text
Supported:
- branch gadget: yes
- indirect call gadget: yes
- global constants: no-op
- string decrypt: no-op

Validation:
- branch: 0x...
- call: 0x...
```

This distinguishes "not needed by this binary" from "not implemented yet".

## Reuse

A binary profile may reuse behavior only by calling stable helper modules or by
explicitly delegating a hook to another named profile:

```python
from . import default

def resolve_branch_gadget(bv, llil, known_targets=None):
    return default.resolve_branch_gadget(bv, llil, known_targets)
```

A profile may not import recovery pass modules directly. The current
`default` profile is the temporary exception because it is the facade for the
existing pass backend; specialized profiles should use helpers or explicit
profile delegation instead.

Do not add profile base classes, factories, mixins, shared profile modules, or
automatic inheritance. The profile module must make hook ownership obvious:
document which hooks reuse another profile and which hooks are binary-specific.

Do not change `profiles/default.py` while adding a new binary profile unless the
task is explicitly to fix the current default binary. Reuse default by calling
its hooks. Widening default behavior for a new binary risks regressing the
existing default binary.

## Helper authoring path

Profile helpers are inspection primitives, not a resolver engine. A resolver
profile still owns binary-specific recognition, target formulas, and the recovery
facts it returns. The recovery backend owns CFG recovery, call-target
application, global slot typing, branch condition translation, IL rewrites, phase
receipts, and cleanup application.

Bundled profiles should import helper modules at module level and call through
the module names:

```python
from ..helpers import facts, llil, memory, mlil
```

This is the stable import surface. Do not import private helper implementation
details, and do not build profile base classes, pattern DSLs, automatic resolver
engines, or external profile loaders around the helpers.
For detailed helper API signatures and behavior, see [`API.md`](API.md).

Use the helper modules by IL level and purpose:

- `llil`: indirect-jump iteration, register definition peeling, and
  `const_values` for PHI-aware constant candidate sets.
- `mlil`: direct/indirect call iteration, variable definition peeling,
  constant/value extraction, single-value constant folding, expression walking
  and operation queries, variable/state-token normalization, address/slot
  extraction, store checks, and cleanup-root discovery.
- `memory`: explicit-width little-endian reads, section checks, and target or
  address validation.
- `facts`: branch, call, global constant, and string decrypt recovery-fact
  builders.

Keep hook code focused on the binary shape. For example, an indirect-call hook
may use MLIL helpers to find a candidate and facts helpers to build the result,
but it should leave call type adjustment and cleanup to workflow:

```python
def resolve_call_gadget(bv, mlil_func):
    out = []
    for call_il in mlil.iter_indirect_calls(mlil_func):
        target = mlil.fold_constant_value(bv, mlil_func, call_il.dest)
        if target is None or not memory.is_call_target(bv, target):
            continue
        roots = mlil.cleanup_roots_for_expr(mlil_func, call_il.dest)
        out.append(facts.call_fact(call_il, target, cleanup_roots=roots))
    return out
```

Cleanup roots are instruction-index sets. They identify top-level IL
instructions that belong to the profile's decode slice, such as assignment
instructions feeding a recovered branch or call target. Expression indices are
backend replacement details used only at the final `replace_expr` site, after the
backend has mapped current SSA/non-SSA IL and decided whether cleanup is safe.

`llil.const_values(bv, ssa, expr)` returns a set of concrete candidates, not a
single value. An empty set means the helper could not recover a concrete value;
multiple values mean the expression has multiple viable candidates, often from a
PHI merge or loop-carried value. If a profile formula needs exactly one table
base, key, or offset, enforce that at the call site:

```python
offsets = llil.const_values(bv, ssa, offset_expr)
if len(offsets) != 1:
    return []
offset = next(iter(offsets))
```

`const_values` does not perform CFG path disambiguation. If one binary can prove
only one PHI edge is feasible, keep that narrowing in its profile or pass.

Global constant helpers provide inspection primitives. They can walk MLIL,
extract constant slot addresses, read qword slots, check sections, detect stores,
and build global constant facts, but the profile or pass still decides which
slot-use shapes, offsets, sections, and resolved addresses are valid. Do not move
an automatic global-constant planner into `helpers`.

String decrypt and deflatten algorithms are not part of the stable helper
surface. Profiles may implement `plan_string_decrypt_calls` and
`plan_deflatten_redirections` using reusable helper primitives, but comment
writing and MLIL rewriting remain backend responsibilities.

## When adaptation fails

Escalate in this order:

1. Recheck the binary facts. Missing or summarized IL is not enough to implement
   a shape safely.
2. Add or tighten one failing test for the hook that misses the shape.
3. Extend the binary profile's private helper.
4. If two binary profiles need the same extension, move it to a stable helper
   module or explicitly delegate the hook to the profile that owns that shape.
5. If the blocker is workflow receipts, cleanup replay, phase ordering, or a BN
   mutation boundary, stop profile work and diagnose or redesign that shared
   contract first.

Do not change `workflow.py` to make one binary profile pass unless the change is
a deliberate general contract change.

## Contract

Each profile module must expose:

```python
PROFILE_ID = "dyzznb_main_202607"
PROFILE_NAME = "DYZZNB main 2026-07"
PROFILE_DESCRIPTION = "Rules for dyzznb_main_202607: branch and call gadgets; no-op globals and strings."

def resolve_branch_gadget(bv, llil, known_targets=None):
    return []

def resolve_call_gadget(bv, mlil):
    return []

def plan_global_constant_slots(bv, mlil):
    return []

def plan_deflatten_redirections(bv, func, mlil):
    return []

def plan_string_decrypt_calls(bv, func, mlil, mlil_stable):
    return []
```

`resolver_profile_from_module()` rejects a module when any required hook is
missing. No-op hooks are valid.

## Recovery facts

`resolve_branch_gadget(bv, llil, known_targets=None)` returns branch facts:

```python
{
    "source": 0x1000,
    "dest_expr_index": 42,
    "targets": (0x2000, 0x3000),
    "newly_resolved": True,
    "cleanup_roots": {123, 124},
}
```

`source` is the indirect branch address. `targets` must contain valid target
addresses. `dest_expr_index` is used only for current-LLIL presentation rewrites;
workflow owns `Function.set_user_indirect_branches`. `cleanup_roots` is optional
and must contain instruction indices rooted in branch-target decode garbage.

`resolve_call_gadget(bv, mlil)` returns call facts:

```python
{
    "call_il": call_il,
    "call_addr": call_il.address,
    "target": 0x5000,
    "decode_def": decode_def,
    "cleanup_roots": {123, 124},
}
```

Workflow owns call type adjustments and cleanup receipt handling. `cleanup_roots`
must be rooted in the call-target decode slice, not in unrelated constants.

`plan_global_constant_slots(bv, mlil)` returns global constant facts:

```python
{
    "slot_addr": 0xA43D70,
    "type": "uint8_t const* const",
    "value": 0x12345678,
    "resolved_addr": 0xA49C30,
    "use_addr": 0x8E127C,
}
```

Workflow owns `BinaryView.define_user_data_var` and global receipts.

`plan_deflatten_redirections(bv, func, mlil)` returns deflatten redirection
plans:

```python
{
    "kind": "uncond",
    "jump": jump_il,
    "target_bb": target_block,
    "obb": original_block,
    "state_var": state_var,
    "state_vars": {state_var},
    "state_tokens": {(0x1234, 4)},
}
```

Profiles recognize the binary-specific dispatcher/state-write shape. Workflow
owns applying those plans through the deflatten backend, recording state tokens
for cleanup, and marking `dispatchthis_mlil_stable`.

`plan_string_decrypt_calls(bv, func, mlil, mlil_stable)` returns string facts:

```python
{
    "call_addr": 0x9000,
    "src_addr": 0xA00000,
    "dst_addr": 0xB00000,
    "plaintext": b"hello",
}
```

Workflow writes comments through the string-decrypt pass. The hook may use
`mlil_stable` to require a decrypt callee to be deflattened first.

## Boundaries

Profiles are pure recognizers. They must not call:

- `Function.set_user_indirect_branches`
- `Function.set_call_type_adjustment`
- `BinaryView.add_analysis_completion_event`
- `BinaryView.define_user_data_var`
- MLIL rewrite APIs such as `replace_expr`, `finalize`, or `generate_ssa_form`
- comment-writing APIs

Those mutations stay in workflow callbacks or existing apply functions so phase
receipts, reanalysis gates, and cleanup invalidation remain centralized.

Profiles must not auto-detect and switch the active profile. If a binary needs a
different profile, set `analysis.plugins.dispatchThis.resolverProfile` explicitly.

## Definition of done

- The binary facts template is complete; unused capabilities are marked `none`.
- The profile has a stable non-sensitive ID, name, and description.
- The capability matrix identifies real hooks and intentional no-ops.
- Every required hook exists, and unused capabilities return `[]`.
- The profile is registered and passes resolver contract validation.
- Every real hook has a focused test.
- `pytest -q` passes.
- Manual BNDB validation is recorded after a full Binary Ninja restart.
- `workflow.py`, `profiles/default.py`, and `passes/` are unchanged unless the
  change explicitly updates a shared contract.
- Profile code, tests, and docs contain no sensitive local/project information.
