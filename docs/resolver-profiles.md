# Resolver profiles

Resolver profiles adapt DispatchThis to one binary without moving workflow
ownership into sample-specific code.

A profile is selected per BinaryView with
`analysis.plugins.dispatchThis.resolverProfile`. Function workflow enablement is
still per function; selecting a profile does not enable DispatchThis for every
function in the view.

## Agent workflow

Do not start by editing code. First collect the sample facts below. If a field is
not used by the sample, write `none`.

```text
sample:
  file:
  architecture:
  platform:
  profile_id:
  profile_name:

capabilities:
  branch_gadget:
  call_gadget:
  global_constants:
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
3. Return `[]` from hooks the sample does not need.
4. Register the module in `plugins/DispatchThis/profiles/__init__.py`.
5. Add one focused test for each non-no-op hook.
6. Run `pytest -q`.
7. Record manual BNDB validation steps and results.
8. Restart Binary Ninja before GUI validation; workflow callback hot reload is
   unreliable.

`llil_excerpt` and `mlil_excerpt` must be raw Binary Ninja IL copied from the
target. Notes may explain the interpretation, but they do not replace the raw
IL. Keep excerpts short, but include the addresses, instruction indices,
variables, and key expressions needed to reproduce the shape.

Keep binary-specific matching in the profile file until two profiles need the
same helper or the profile becomes hard to read. Put shared profile helpers in
`profiles/_shared.py` or `profiles/<family>_shared.py`. Shared `passes/` code is
for stable workflow-level capabilities, not for one binary or speculative reuse.

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

A binary profile may explicitly delegate a hook to `default` or to a shared
profile helper:

```python
from . import default

def resolve_branch_gadget(bv, llil, known_targets=None):
    return default.resolve_branch_gadget(bv, llil, known_targets)
```

Do not add profile base classes, factories, mixins, or automatic inheritance.
The profile module must make hook ownership obvious: document which hooks reuse
default behavior and which hooks are binary-specific.

Do not change `profiles/default.py` while adding a new binary profile unless the
task is explicitly to fix the current default binary. Reuse default by calling
its hooks. Widening default behavior for a new binary risks regressing the
existing default binary.

## When adaptation fails

Escalate in this order:

1. Recheck the sample facts. Missing or summarized IL is not enough to implement
   a shape safely.
2. Add or tighten one failing test for the hook that misses the shape.
3. Extend the binary profile's private helper.
4. If two binary profiles need the same extension, move it to
   `profiles/_shared.py` or `profiles/<family>_shared.py`.
5. If the blocker is workflow receipts, cleanup replay, phase ordering, or a BN
   mutation boundary, stop profile work and diagnose or redesign that shared
   contract first.

Do not change `workflow.py` to make one binary profile pass unless the change is
a deliberate general contract change.

## Contract

Each profile module must expose:

```python
PROFILE_ID = "sample_x"
PROFILE_NAME = "Sample X"
PROFILE_DESCRIPTION = "Rules for the Sample X family."

def resolve_branch_gadget(bv, llil, known_targets=None):
    return []

def resolve_call_gadget(bv, mlil):
    return []

def plan_global_constant_slots(bv, mlil):
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
}
```

`source` is the indirect branch address. `targets` must contain valid target
addresses. `dest_expr_index` is used only for current-LLIL presentation rewrites;
workflow owns `Function.set_user_indirect_branches`.

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
- comment-writing APIs

Those mutations stay in workflow callbacks or existing apply functions so phase
receipts, reanalysis gates, and cleanup invalidation remain centralized.

Profiles must not auto-detect and switch the active profile. If a sample needs a
different profile, set `analysis.plugins.dispatchThis.resolverProfile` explicitly.

## Human checklist

Before accepting a new profile:

- The profile has a stable `PROFILE_ID`, name, and description.
- Every required hook exists.
- Unused capabilities return `[]`.
- Every non-no-op hook has a focused test.
- The profile is registered explicitly in `_PROFILES`.
- The sample's supported and unsupported capabilities are listed in the profile
  docstring or module comments.
- `pytest -q` passes.
- Manual BNDB validation checks at least one enabled function after workflow
  completion.
- GUI validation used a full Binary Ninja restart after workflow/profile edits.

Manual BNDB validation does not need to run in CI, but it must be written down
for the profile change. Check the capabilities that profile claims: branch
targets submitted, indirect calls rewritten to direct calls, phase cleanup
residue removed, global slots typed, string decrypt comments written, or no-op
hooks leaving analysis untouched.

## Definition of done

- The sample facts template is complete; unused capabilities are marked `none`.
- The profile has a stable non-sensitive ID, name, and description.
- The capability matrix identifies real hooks and intentional no-ops.
- The profile is registered and passes resolver contract validation.
- Every real hook has a focused test.
- `pytest -q` passes.
- Manual BNDB validation is recorded after a full Binary Ninja restart.
- `workflow.py`, `profiles/default.py`, and `passes/` are unchanged unless the
  change explicitly updates a shared contract.
- Profile code, tests, and docs contain no sensitive local/project information.
