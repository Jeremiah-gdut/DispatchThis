# The obfuscation

This document describes the ARM64 ELF obfuscation shapes that DispatchThis
targets today. Concrete binary quirks belong in resolver profiles; this page
only documents the shared recovery model.

## High-level shape

Control flow is flattened: original basic blocks no longer branch directly to
one another. Instead, a dispatcher compares a state variable against opaque
state tokens and routes execution to the next original basic block.

Run-time flow for one transition:

```text
original block -> set state = <next token> -> decode gadget -> jump dispatcher
               -> compare tree dispatcher -> next original block
```

DispatchThis rebuilds this at the IL level. It recovers state-token targets,
rewrites dispatcher-controlled exits to direct MLIL edges, and leaves the
underlying bytes untouched.

## State variable and dispatcher

The dispatcher is a compare tree over one state variable. Each comparison maps a
state token to the original basic block that token selects. State tokens may be
wider than 32 bits, so the token width is part of the token identity.

The deflattener identifies the dominant dispatcher comparison cluster, maps
`(state_token, width)` values to target blocks, then follows state writes in each
original basic block to recover direct successors.

## Decode-gadget indirect branches

Original blocks return to the dispatcher through a decode gadget rather than a
direct branch. The ARM64 resolver profile parses the LLIL dataflow that computes
the branch target and returns branch recovery facts to the workflow.

The current bundled branch formula is:

```text
table_base = (*slot + table_base_key) mod 2^48
entry      = *(table_base + entry_offset)
target     = (entry + key) mod 2^48
```

`slot`, `table_base_key`, `entry_offset`, and `key` are recovered from LLIL
definitions and image memory. Profiles may recognize different instruction
shapes, but they still return standard branch facts; workflow owns the Binary
Ninja branch metadata and reanalysis receipts.

## Indirect call gadgets

Indirect call recovery runs on MLIL after branch resolving is stable. The current
shape folds an encoded callee value plus a key:

```text
target = (encoded + key) mod 2^48
```

When the result is a valid callee, the pass rewrites the current MLIL call
destination to a constant pointer. Workflow owns call type adjustments and
receipt gating so repeated analysis does not loop forever.

## Global constant slots

Some samples store pointer-like constants in writable global slots. The global
constant phase recognizes narrow, read-only slot-use shapes and asks workflow to
type the slot as `uint8_t const* const`. This lets later MLIL dataflow treat the
slot as stable without moving BinaryView mutations into resolver profiles.

## String decrypt calls

String decrypt recovery is opt-in. It scans direct MLIL calls in the current
function, requires the decrypt callee to be already deflattened and stable, then
lets the active profile return plaintext recovery facts. The backend writes
function-level comments while preserving existing manual comment lines.

## Conditional transitions

Most transitions write one state token before returning to the dispatcher. Some
write one of two state tokens based on program control flow. DispatchThis
handles the narrow MLIL shape where each pure branch arm writes exactly one known
state token; unsupported or impure shapes remain intact. See
[`conditional-deflattening.md`](conditional-deflattening.md).
