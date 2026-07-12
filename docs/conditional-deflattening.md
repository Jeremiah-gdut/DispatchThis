# Conditional deflattening

Most flattened transitions are unconditional: an original basic block writes one
dispatcher state token and jumps back to the dispatcher. DispatchThis rewrites that
terminator into a direct `goto` to the target block for that token.

Some transitions are conditional. The current pass handles the narrow shape where an
original basic block contains an `MLIL_IF` and all state writes in each arm resolve to one
known dispatcher state token before returning to the dispatcher. Rewrites that skip arm
work require pure state-selection code. A private shared-exit rewrite may instead
preserve modeled semantic work throughout both arms and the merge because it changes only
their common final dispatcher `GOTO`.

The original transition condition and the dispatcher predicates are separate. The
original `MLIL_IF` may use any condition because the rewriter copies it unchanged. The
dispatcher may route with variable/constant equality, inequality, or signed/unsigned
`LT`, `LE`, `GT`, and `GE` comparisons.

## What The Analysis Recovers

`compute_redirections` first identifies the dominant dispatcher comparison cluster. For
each concrete `(state_token, width)` recovered from an original region, it evaluates each
dispatcher predicate in CFG order, preserving comparison operand order and signedness,
until the route reaches one target block. This supports compare trees built from ordering
predicates without constructing symbolic token intervals.

For a candidate original basic block, the conditional planner:

- finds an `MLIL_IF` inside the original basic block region;
- walks the true and false regions until the dispatcher boundary;
- for rewrites that bypass work, rejects assignments outside the state-selection
  dependency chain or whose assigned variables remain live outside the arm scope;
- requires each dispatcher comparison alias to be defined by a unique,
  equal-width whole-variable direct-copy chain earlier in that comparison row,
  ending at the shared state input;
- when the IF consumes a predicate variable, requires its resolved comparison
  definition to map from SSA to the exact current non-SSA instruction in the
  same row and proves the state copies occur before that comparison rather than
  merely before the IF;
- requires every state write in an arm to resolve and agree on one concrete token;
- proves every path establishes that token before reaching the dispatcher;
- requires every CFG path in each arm to terminate at a dispatcher entry, then
  replays the token through every such entry and requires one target original block;
- rejects the original region rather than choosing by block order when more
  than one conditional candidate is valid;
- requires the complete rewritten arm to have no foreign entry and requires
  pointer-based state definitions to dominate their STORE uses;
- records only exact current-MLIL state-write instruction indices proved obsolete by the
  recovered transition.

If both arms resolve to different known targets, `rewrite_redirections_mlil` chooses one
of three proved rewrites. It may copy the candidate `MLIL_IF` condition when the skipped
state channel is dispatcher-only and privately owned. When each arm has a distinct GOTO
directly into a dispatcher comparison row, it rewrites those arm exits and leaves the
original condition and state writes on the execution path. When both arms converge into
one private merge tail, it preserves the original IF, both arms, and the complete shared
tail, then replaces only the shared final GOTO with an IF on the
already-written state token. This `shared_exit` mode never marks state writes obsolete.
All modes use copied source-block labels. Their edge rewrites and the plan's exact
state-write NOPs are one atomic copy-transform; the whole replacement is discarded if
any selected rewrite cannot be emitted.

Cleanup proof is deliberately weaker than target proof. If both targets are proved but no
state-write instruction can be proved obsolete, the plan keeps an empty
`obsolete_state_writes` set and still reconstructs the conditional CFG.
Non-empty cleanup also carries exact current-MLIL write witnesses; stale owner or
operand evidence rejects the whole copy instead of NOPing an instruction that merely
reused the index.
This applies only when the selected rewrite preserves execution of those writes.
If distinct arm-exit rewrites are unavailable, cleanup or ownership uncertainty
rejects the IF shortcut because an empty set cannot make bypassed writes execute.
An external entry rejects every mode because rewriting an arm or shared exit would also
redirect the foreign path without target proof for that entry.

## Limits

This is intentionally narrower than a symbolic predicate rebuild. It does not try to
solve state ranges, variable/variable comparisons, or arbitrary multi-step state-selection
chains. Modeled semantic work is accepted only for shared-exit mode, when the entire
arm-and-merge region is private, every path establishes a concrete token, and both routes
use one final GOTO. Possible state mutations, a token-width mismatch, or an ambiguous route reject the
transition. Unsupported shapes are left intact for Binary Ninja to display normally.
Implicit dispatcher pass-through expansion is limited to `NOP* + GOTO` routing.
Direct copies are accepted only inside an explicitly proved comparison row or
the unique shared state latch; unrelated assignments or externally observed
dispatcher temporaries are not bypassed. The latch must be an equal-width
whole-variable chain shared by at least two independent target-head regions, so
an OBB-local conditional state-selection join remains part of its OBB.
Field, split, aliased, and unresolved struct/pointer writes that may modify the
state channel are not ignored: the affected transition is left flattened.
Taking either the whole state address or a field address also counts as an
observer/escape unless the profile proves the narrow owned-store exception.
Passing such an address, including through a followed pointer expression, to a
call, syscall, or intrinsic invalidates the concrete transition token.
Field/split/aliased reads and `vars_read` metadata also count as observers, but
never as exact whole-state copies. Once an address is stored into memory, a later
unknown call invalidates the token even if the call has no explicit pointer
argument.
An unknown operation receiving `&holder` also counts when the holder contains
`&state`; after escape, traps, breakpoints, and non-exact stores are unknown
mutations. Unimplemented MLIL rejects a transition outright. Variables are
matched by Binary Ninja identity/equality rather than display name, and an
auxiliary comparison block is excluded from observer checks only after its full
routing prefix is proved pure.
