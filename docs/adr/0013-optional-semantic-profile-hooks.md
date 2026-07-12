# ADR 0013: Keep semantic profile hooks optional

## Status

Accepted.

## Context

Profiles adapt known sample-specific recognition and formulas to stable workflow
backends. Requiring all six hooks made a profile repeat no-op functions and forwarding
wrappers even when a sample used only one capability. Repeating branch coordinates in a
fact also allowed the supplied address and expression index to disagree with its LLIL
witness.

## Decision

Keep the six operation-specific hook names because their LLIL/MLIL inputs and recovery
facts express distinct semantics. Profile metadata is mandatory, but capability hooks are
optional. A missing hook means unsupported and the registry exposes one shared
empty-result function; an attribute that exists but is not callable is an error. Profiles
reuse identical behavior with direct function aliases and use wrappers only when behavior
or arguments change.

Fact builders derive repeated coordinates from exact current-IL witnesses. In particular,
`branch_fact(jump_il, targets, ...)` derives `source` and `dest_expr_index` from
`jump_il`.

Do not replace these hooks with `recover(request)`, a profile base class, inheritance,
dynamic profile detection, or a pattern DSL. Profiles remain pure recognizers, workflow
callbacks retain every reanalysis-triggering mutation, and the existing fact/plan
backends remain the orchestration boundary.

## Consequences

- A profile implements only sample-specific capabilities and contains less adapter code.
- Capability matrices must mark hooks as custom, aliased, or omitted so a typo is not
  mistaken for an intentional omission during review.
- Direct aliases bind at import time; a full Binary Ninja restart is required after
  profile registration or callback changes.
- New abstraction is deferred until multiple real profiles demonstrate the same missing
  semantic operation rather than merely sharing syntax.
