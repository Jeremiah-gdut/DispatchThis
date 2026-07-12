# Resolver profiles

Resolver profiles adapt DispatchThis to one binary without moving workflow
ownership into profile-specific code.

A profile is selected per BinaryView with
`analysis.plugins.dispatchThis.resolverProfile`. Function workflow enablement is
still per function; selecting a profile does not enable DispatchThis for every
function in the view.

Function phase state records the profile ID that produced its recovery evidence.
The UI refuses to switch profiles while any function in the view still contains
branch, call, cleanup, or global recovery evidence. Workflow also fails closed on
legacy evidence without provenance or evidence bound to another profile. Empty
function state may be rebound because it contains no analysis claim to reuse.

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
  correlated_stores:
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

correlated_stores:
  join_store_addr:
  mlil_excerpt:
  notes:
  predecessor_arms:

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
2. Define the metadata and only the semantic hooks this binary supports.
3. Omit unsupported hooks; the registry normalizes them to an empty result.
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

Every binary profile must declare which semantic hooks are custom, aliases, or
intentionally omitted. Keep this near the top of the profile module as a comment:

```text
Supported:
- branch gadget: custom
- indirect call gadget: alias valorant_2_6
- global constants: custom
- correlated stores: omitted
- deflatten: alias default
- string decrypt: omitted

Validation:
- branch: 0x...
- call: 0x...
```

This distinguishes "not needed by this binary" from "not implemented yet".

## Reuse

A binary profile may reuse behavior only through stable helper modules or by
explicitly aliasing a hook from another named profile:

```python
from . import default

resolve_branch_gadget = default.resolve_branch_gadget
```

Use a wrapper only when the profile intentionally changes arguments or behavior.

A profile may not import recovery pass modules directly. The current
`default` profile is the temporary exception because it is the facade for the
existing pass backend; specialized profiles should use helpers or explicit
profile delegation instead.

Do not add profile base classes, factories, mixins, shared profile modules, or
automatic inheritance. The profile module must make hook ownership obvious:
document which hooks reuse another profile and which hooks are binary-specific.

Do not change `profiles/default.py` while adding a new binary profile unless the
task is explicitly to fix the current default binary. Reuse default by aliasing
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
  and operation queries, variable/state-token normalization, concrete dispatcher
  comparison parsing/evaluation, address/slot extraction, store checks, and
  cleanup-root discovery.
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
        if target is None or not memory.is_known_callee(bv, target):
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

`llil.const_values(bv, ssa, expr)` returns a complete set of concrete candidates,
or `None` when any semantic path is unknown. Multiple values mean the expression
has several viable candidates, often from a PHI merge or loop-carried value. If
a profile formula needs exactly one table base, key, or offset, enforce both
completeness and cardinality at the call site:

```python
offsets = llil.const_values(bv, ssa, offset_expr)
if offsets is None or len(offsets) != 1:
    return []
offset = next(iter(offsets))
```

`const_values` does not perform CFG path disambiguation. A profile may narrow a
PHI only from complete binary-specific path evidence; it must never keep the
known arms of an otherwise unknown result.

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

Each profile module must expose metadata:

```python
PROFILE_ID = "dyzznb_main_202607"
PROFILE_NAME = "DYZZNB main 2026-07"
PROFILE_DESCRIPTION = "Rules for dyzznb_main_202607 branch and call gadgets."
```

It may expose any of these six semantic capability hooks:

```python

def resolve_branch_gadget(bv, llil, known_targets=None):
    return []

def resolve_call_gadget(bv, mlil):
    return []

def plan_global_constant_slots(bv, mlil):
    return []

def plan_correlated_store_rewrites(bv, func, mlil):
    return []

def plan_deflatten_redirections(bv, func, mlil):
    return []

def plan_string_decrypt_calls(bv, func, mlil, mlil_stable):
    return []
```

Missing hooks mean that the profile does not support that capability;
`resolver_profile_from_module()` supplies one shared empty-result function to
the workflow-facing `ResolverProfile`. A hook attribute that exists but is not
callable is rejected as a profile error. This keeps hook names semantic without
requiring boilerplate no-op functions, a profile base class, or a dispatch DSL.

## Recovery facts

`resolve_branch_gadget(bv, llil, known_targets=None)` returns branch facts:

```python
{
    "source": 0x1000,
    "dest_expr_index": 42,
    "targets": (0x2000, 0x3000),
    "cleanup_roots": {123, 124},
    "jump_il": jump_il,
}
```

Build the fact from the exact current LLIL witness so repeated coordinates cannot
disagree:

```python
return facts.branch_fact(jump_il, targets, cleanup_roots=cleanup_roots)
```

`source` is derived from `jump_il.address`, while `dest_expr_index` is derived
from `jump_il.dest.expr_index`. `targets` must contain valid target addresses.
`dest_expr_index` is used only for current-LLIL presentation rewrites;
workflow owns `Function.set_user_indirect_branches`. `cleanup_roots` is optional
and must contain instruction indices rooted in branch-target decode garbage.
Bundled resolvers also retain the exact current `jump_il` witness; the backend
rejects stale, missing, or same-source conflicting witnesses before any rewrite
or metadata submission. Workflow supplies `known_targets` only for receipts whose
complete target tuples exactly match Binary Ninja's current non-auto user branch
metadata. A resolver may skip those sources as an already verified frontier, but
must freshly recognize every other source. Callers must not pass an unverified
cache: receipt-only, missing, automatic, subset, superset, or changed metadata is
not `known_targets`, and it is never a fallback for failed current decoding.

`resolve_call_gadget(bv, mlil)` returns call facts:

```python
{
    "call_il": call_il,
    "call_addr": call_il.address,
    "target": 0x5000,
    "decode_def": decode_def,
    "cleanup_roots": {123, 124},
    "cleanup_load_roots": {123},
}
```

Workflow owns call type adjustments and cleanup receipt handling. `cleanup_roots`
must describe the call-target decode slice, not unrelated constants. The backend
rebinds `call_il` and the descriptive `decode_def` to exact current non-SSA MLIL, but
rewrites only `call_il.dest`. Before cleanup it replaces both root sets with the exact current SSA
reaching-definition slice. PHIs are followed completely; partial, split, or aliased
definitions disable cleanup. `cleanup_load_roots` is optional and must be a subset of
`cleanup_roots`; it permits SSA-dead load assignments to be removed without treating
arbitrary loads as pure or consulting incomplete xrefs. Profile-supplied instruction
indices are advisory only and never authorize a mutation after reanalysis.

`plan_global_constant_slots(bv, mlil)` returns global constant facts:

```python
{
    "slot_addr": 0xA43D70,
    "type": "uint8_t const* const",
}
```

Evidence relevant to the binary shape, such as an observed value, resolved
address, or use site, remains private to the profile.
Workflow owns `BinaryView.define_user_data_var` and function global-phase
receipts.

`plan_correlated_store_rewrites(bv, func, mlil)` returns plans for moving a
join-block store back into the predecessor arms that own its correlated values:

```python
{
    "store": store_il,
    "size": 4,
    "arms": (
        {"goto": true_goto, "dest": 0xA000, "src": 0xB000},
        {"goto": false_goto, "dest": 0xB000, "src": 0xA000},
    ),
}
```

The profile proves the PHI-arm correlation and concrete addresses. The backend
owns the atomic MLIL copy-transform.

`plan_deflatten_redirections(bv, func, mlil)` returns deflatten redirection
plans:

```python
{
    "kind": "uncond",
    "exit_jumps": (jump_il,),
    "target_bb": target_block,
    "obb": original_block,
    "state_token": (0x1234, 4),
    "obsolete_state_writes": {123},
}
```

Profiles recognize the binary-specific dispatcher/state-write shape. Every
unconditional plan must include all private dispatcher exits for that original
region, and concrete token replay from every exit must prove the same target.
For conditional plans, every path in each arm must terminate at a dispatcher
entry and establish the same token. Work bypassed by a rewrite must stay on the
state-selection dependency chain; modeled semantics may remain in a private
shared-exit region because that whole region still executes. Multiple valid
candidates must be rejected rather than ordered implicitly.
Supported variable/constant dispatcher comparisons are `E`, `NE`, and signed or
unsigned `LT`, `LE`, `GT`, and `GE`; comparison operand order and token width are
part of the evidence. Profiles do not need to solve symbolic intervals.

`obsolete_state_writes` is a `set[int]` of exact current-MLIL instruction
indices proved redundant because of that plan's redirection. Target proof and
cleanup proof are independent: an uncertain target produces no plan, while
uncertain cleanup produces a valid plan with an empty set. The backend validates
and applies every selected exit/conditional rewrite and every exact NOP in one
atomic copy-transform. Workflow publishes `dispatchthis_mlil_stable` only after
installing that replacement; it does not publish token/variable cleanup maps.
An external entry into a selected conditional arm rejects the plan because the
exit mutation would affect an unproved foreign path. Profiles that recognize
state stores through pointers must require one complete, unique definition
chain to `&state`, with each definition dominating its use; historical
assignment to a pointer variable is not sufficient.

Conditional plans set `rewrite_mode` to `arm_exits` when `exit_targets` can
redirect distinct arm GOTOs directly from the state-selection tails while
preserving their execution. `rewrite_mode: condition` shortcuts `if_il` and is
valid only with complete private cleanup plus proof that the skipped state
channel has no non-dispatcher observer. Profiles must reject a condition
shortcut when that stronger proof is unavailable.

Profiles must not classify arbitrary assignment blocks as dispatcher routing.
Only NOP/GOTO routing and direct copies among proved state-dependency variables
may be replayed as token-preserving. Selected comparison rows obey the same
restriction, and dispatcher-derived temporaries must not be observed outside
the dispatcher.

The comparison variable must have one unique, equal-width whole-variable
`MLIL_VAR`/`MLIL_VAR_SSA` direct-copy chain
earlier in its own dispatcher row. All selected rows must end those chains at
the same state input. A definition elsewhere that merely traces back to state is
not sufficient because the comparison may consume a stale value. Treat
`SET_VAR_FIELD`, `SET_VAR_SPLIT`, aliased writes, and `STORE_STRUCT` as possible
state mutations; if the resulting token cannot be proved exactly, reject the
transition. Treat `ADDRESS_OF_FIELD` as an address escape just like
`ADDRESS_OF`.

If an IF condition is a predicate variable, map its SSA definition through
`non_ssa_form`, verify the exact current non-SSA instruction earlier in the same
row, and use that comparison instruction
as the copy-chain use point. A state copy performed after the comparison cannot
justify replay. Calls, syscalls, and intrinsics that receive a possible state
pointer invalidate the token even if an earlier write was constant. A state
address stored into memory also makes later unknown calls possible mutations,
even without an explicit pointer argument. Exact zero-offset pointer copies may
be supported when their unique, width-preserving definition chain dominates the
store; field values, truncating copies, and other pointer arithmetic remain
possible mutations and must reject the transition.

Profiles and shared helpers must key variables/registers by Binary Ninja's real
identity/equality, not by display names from `str` or `repr`. Auxiliary
comparison rows may be treated as dispatcher blocks only after their full prefix
passes routing-purity validation; otherwise they remain visible to observer
proofs. Address escape includes a pointer stored in memory or retained through
an addressed holder. After escape, unknown effects and non-exact stores reject
the transition; `MLIL_UNIMPL` and `MLIL_UNIMPL_MEM` reject unconditionally.

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
- The capability matrix identifies custom hooks, aliases, and omitted capabilities.
- Every declared hook is callable; unsupported capabilities are omitted.
- The profile is registered and passes resolver contract validation.
- Every real hook has a focused test.
- `pytest -q` passes.
- Validation on a freshly opened raw binary is recorded after a full Binary Ninja restart.
- `workflow.py`, `profiles/default.py`, and `passes/` are unchanged unless the
  change explicitly updates a shared contract.
- Profile code, tests, and docs contain no sensitive local/project information.
