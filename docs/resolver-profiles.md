# Resolver profiles

Resolver profiles adapt DispatchThis to a sample family without moving workflow
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

branch_gadget:
  unresolved_jump_addr:
  llil_excerpt:
  decoded_targets:

call_gadget:
  indirect_call_addr:
  mlil_excerpt:
  decoded_callee:
  cleanup_roots:

global_constants:
  slot_addr:
  mlil_excerpt:
  resolved_addr:

string_decrypt:
  call_addr:
  callee_addr:
  source_blob_addr:
  expected_plaintext:

validation:
  pytest:
  bn_commands:
```

Then implement the smallest profile that satisfies those facts:

1. Add `plugins/DispatchThis/profiles/<profile_id>.py`.
2. Define the metadata and every required hook.
3. Return `[]` from hooks the sample does not need.
4. Register the module in `plugins/DispatchThis/profiles/__init__.py`.
5. Add one focused test for each non-no-op hook.
6. Run `pytest -q`.
7. Restart Binary Ninja before GUI validation; workflow callback hot reload is
   unreliable.

Keep sample-specific matching in the profile file until two profiles need the
same helper or the profile becomes hard to read. Shared `passes/` code is for
proven reuse, not for a single new sample.

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
- GUI validation used a full Binary Ninja restart after workflow/profile edits.
