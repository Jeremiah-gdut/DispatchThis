# DispatchThis

DispatchThis is a Binary Ninja workflow plugin for recovering readable control flow from an ARM64 ELF obfuscation family.

## Language

**Sample family**:
A set of obfuscated ARM64 ELF binaries that share enough transformation patterns
to reuse profile helpers or implementation patterns.
_Avoid_: generic target

**Decode gadget**:
A short obfuscation sequence that computes a control-flow or call target from encoded data at runtime.

**Indirect branch resolving**:
Recovering the concrete target of a computed jump so analysis can discover the next block.
_Avoid_: deinbr

**Resolver profile**:
A focused recognizer for one specific binary's obfuscation shapes. Multiple
binary profiles may share helpers when their sample family behavior overlaps.
_Avoid_: generic rule engine

**Resolver profile contract**:
The narrow agreement a resolver profile must satisfy: declare its metadata and
implement only the semantic capabilities it supports for one binary's indirect
branch, indirect call, global constant, correlated-store, deflatten, and
string-decrypt shapes, then return standard recovery facts or plans without
owning workflow mutations. Missing hooks are normalized to an empty result by
the registry; identical behavior is expressed with a direct function alias. The plan hooks are
`plan_correlated_store_rewrites`, `plan_deflatten_redirections`, and
`plan_string_decrypt_calls`.
_Avoid_: middleware, adapter framework, plugin rewrite layer

**Binary profile**:
A resolver profile whose default ownership boundary is one concrete binary or
BNDB. It may delegate shared behavior to helpers, but profile selection remains
explicit per BinaryView.
_Avoid_: per-family profile, automatic detector

**Profile ID**:
A stable lowercase snake_case identifier for a binary profile. It should be
traceable to the binary without exposing local paths, usernames, customer names,
or other sensitive project labels.
_Avoid_: sample1, current, default2, full local paths

**Profile provenance**:
The resolver profile ID stored with function-scoped workflow evidence. Empty
state may bind to the active profile; state containing recovery evidence cannot
be rebound or reused under a different profile.
_Avoid_: implicit profile migration, unowned legacy receipts

**Profile helper**:
A reusable BNIL or BinaryView inspection helper used by resolver profiles and
passes to collect definitions, fold constants, read target data, validate
addresses, or build recovery facts. Profile helpers reduce per-binary resolver
code, but they do not own binary-specific recognition or workflow mutations.
_Avoid_: utils, generic rule engine, backend

**MLIL helper primitive**:
A stable profile helper in `helpers.mlil` that exposes reusable Medium Level IL
inspection behavior, such as call iteration, expression scalar-value extraction,
expression operation queries, or variable-definition-aware expression traversal.
It is not a binary-specific recognizer, pattern DSL, resolver engine, or recovery
backend mutation point.
_Avoid_: string decrypt helper, pattern rule, resolver engine

**Expression scalar value**:
A direct MLIL constant or Binary Ninja single-value result recovered from one
expression without arithmetic folding, memory reads, PHI candidate expansion, or
target validation.
_Avoid_: full value engine, constant folder

**Expression operation query**:
A helper-level check for whether an MLIL expression tree, optionally including
followed variable definitions, contains one of a caller-provided set of native
MLIL operation enums or enum-derived compatibility names.
_Avoid_: pattern matcher, string decrypt recognizer

**Active resolver profile**:
The resolver profile explicitly selected for a BinaryView. It chooses how enabled
functions interpret that binary's obfuscation shapes; it does not enable the
workflow for every function in the view.
_Avoid_: automatic sample detection

**Default resolver profile**:
The bundled resolver profile named `default`, representing the current binary
rules shipped with DispatchThis. The name does not mean generic support for every
binary or obfuscation family.
_Avoid_: current_arm64, universal profile

**Function workflow enablement**:
The per-function opt-in setting that decides whether DispatchThis workflow phases
run for that function. It is separate from the BinaryView's active resolver profile.
_Avoid_: whole-view workflow application

**Recovery fact**:
A standard piece of recovered analysis information returned by a resolver profile,
such as an indirect branch target, indirect call target, or global constant slot.
Workflow callbacks decide how and when to submit recovery facts to Binary Ninja.
_Avoid_: profile action, Binary Ninja mutation request

**Recovered target set**:
The complete set of concrete control-flow destinations supported by current
recovery evidence. Consumers preserve every target unless the evidence proves
that exactly one target is valid.
_Avoid_: first target, preferred target

**Implicit target pruning**:
Silently selecting one member of a recovered target set without semantic proof
that the other members are invalid. It can erase valid CFG edges and is never a
safe single-target convenience.
_Avoid_: pick first, best-effort target

**Verified branch frontier**:
The branch sources whose complete receipt target tuples exactly match Binary
Ninja's current non-auto user branch metadata. Only these sources may be omitted
from an incremental recognition run; receipt-only, automatic, missing, subset,
superset, or changed mappings remain in the decode frontier.
_Avoid_: cached branches, assumed-resolved sources

**Complete value evidence**:
Every concrete value supported by every semantic path of one BNIL expression.
A helper returns the complete set or an explicit unknown result; an unsupported
arm, bounded-expansion miss, unreadable load, or invalid target never licenses a
known-subset result.
_Avoid_: best-effort values, known arms only

**Current IL witness**:
The Binary Ninja instruction object retained by a recovery plan and rebound at
the mutation boundary by exact operation, address, instruction/expression
indices, relevant operands, and owning IL function. A similar instruction found
by scanning is not the same witness.
_Avoid_: nearby match, stale plan object

**Mixed-IL operation-name seam**:
The narrow compatibility boundary where a matcher intentionally handles both
LLIL and MLIL and therefore compares enum-derived names to avoid equal `IntEnum`
values crossing levels. Single-IL modules use Binary Ninja operation enums.
_Avoid_: hand-written MLIL_* string, hand-written LLIL_* string

**Global constant recovery fact**:
A recovery fact that identifies a global data slot and the const-qualified type it
should receive. Values, resolved addresses, and use sites that justified recognition
remain resolver-profile evidence rather than part of the recovery fact.
_Avoid_: global constant audit record, global constant mutation receipt

**Recovery backend**:
The workflow or pass layer that consumes recovery facts and applies stable
Binary Ninja analysis effects, such as CFG recovery, call-target application, IL
translation, global slot typing, or cleanup. Resolver profiles and profile
helpers feed the backend; they do not replace it.
_Avoid_: profile helper, generic rule engine

**Indirect call resolving**:
Recovering the concrete callee of a computed call target.
_Avoid_: deincall

**Call-target definition slice**:
The complete current SSA reaching-definition chain feeding `call.dest`, including every
PHI input. Only exact whole-variable SSA definitions mapped to current non-SSA assignments
belong to the slice. Once the destination is replaced, only assignments in this owned
slice whose SSA values have no other consumers may be cleaned; xrefs do not define
ownership.
_Avoid_: preceding instructions, whole-function dead-load scan

**Global constant resolving**:
Recovering read-only semantics for global data slots that the sample family stores in writable sections but uses as constants.
_Avoid_: global variable fixing, data constant propagation

**String decrypting**:
Recovering plaintext strings from the sample family's encoded byte blobs.
_Avoid_: generic string deobfuscation

**String decrypt function**:
A sample-family decoder clone that writes one plaintext string to a caller-provided buffer and marks a one-shot done flag.
_Avoid_: string helper, generic decoder

**Decrypted string comment**:
A Binary Ninja call-site comment containing the plaintext recovered for a string decrypt function invocation.
_Avoid_: recovered string literal

**String decrypt recovery fact**:
The standard recovered information for one string decrypt call site: call address,
source blob address, destination buffer address, and plaintext bytes. Workflow
code owns turning it into a decrypted string comment.
_Avoid_: comment plan, profile annotation

**Dispatcher**:
The flattened control-flow router that chooses the next original block from a state value.

**Dispatcher cluster**:
A connected set of dispatcher comparison blocks that route by comparing state
tokens. Identify it from variable/constant equality, inequality, or signed or
unsigned ordering comparisons whose variables have unique row-local direct-copy
chains ending at the same state input; graph shape validates the cluster but is
not the primary signal.

**Dispatcher comparison chain**:
The unique sequence of direct, equal-width variable copies earlier in one
dispatcher row that feeds its comparison and ends at the shared state input.
An arbitrary definition elsewhere that traces to state is not equivalent because
the comparison value may be stale on another entry path.

**Exact whole-variable read**:
An `MLIL_VAR` or `MLIL_VAR_SSA` use of an entire variable. Field, split, and
aliased forms remain important may-read/may-alias evidence, but they are not
proof that a dispatcher comparison or pointer copy carries the complete value.
_Avoid_: treating `VAR_FIELD` as a direct copy

**Variable identity**:
The equality/identity of Binary Ninja's underlying Variable, SSAVariable, or
register object after explicit wrapper normalization. A display name is not
identity because distinct storage objects can render the same `str`/`repr`.
_Avoid_: string-keyed bindings, same-name fallback

**Resolved dispatcher predicate**:
A predicate-variable IF whose defining comparison is a current instruction
earlier in the same dispatcher row. Copy-chain ordering is measured at that
comparison, not at the later IF, so a post-comparison state copy is never replay
evidence.

**State address escape**:
Publishing `ADDRESS_OF` or `ADDRESS_OF_FIELD` of the dispatcher state, directly
or through variable/holder definitions, into memory or an unknown
memory-effecting operation. Once stored or retained, a later unknown operation
or non-exact store can recover and mutate the state even without an explicit
pointer argument.

**Concrete dispatcher replay**:
Routing one recovered `(state_token, width)` through the actual dispatcher CFG by
evaluating each comparison with its original operand order and bitvector
signedness. This proves a target for that token without constructing symbolic
state intervals.
_Avoid_: symbolic range recovery, assumed comparison arm

**State variable**:
The value consumed by the dispatcher to select the next original block.

**Dispatcher comparison variable**:
The row-local/root value read by dispatcher comparisons. It is normally the
state variable itself. When every dispatcher ingress passes through one proved
equal-width whole-variable latch, it may be a refreshed copy of the transition
state variable.

**Shared state latch**:
The unique dispatcher-ingress copy chain that refreshes the dispatcher
comparison variable from the variable written by original blocks. It is a
dispatcher boundary only when at least two independent dispatcher target-head
regions own distinct writes feeding it; an OBB-local state-selection join is not
a shared latch.

**State token**:
The opaque dispatcher value compared against the state variable. Its bit width is
part of its identity; do not assume all state tokens are 32-bit.

**Original basic block**:
A block from the original control flow before flattening redirected it through the dispatcher.
_Avoid_: OBB outside short code comments

**Deflattening**:
Reconnecting original basic blocks directly after dispatcher-controlled successors are recovered.

**Obsolete state write**:
A dispatcher-state assignment or store proved unnecessary because its owning
transition is redirected directly to the recovered successor. A deflatten plan
identifies it by exact current-MLIL instruction index; matching token values or
variables elsewhere are not cleanup evidence.
_Avoid_: state-token scan, function-wide cleanup match

**Atomic deflatten replacement**:
One MLIL copy-transform containing every selected dispatcher-exit or conditional
rewrite and every exact obsolete-state-write NOP. If any selected rewrite cannot
be applied, none of the replacement is installed.

**Unconditional transition**:
A recovered original-block successor selected by one concrete state token; the
region may contain multiple state writes only when all of them resolve to that
same token.

**Conditional transition**:
A recovered original-block successor set selected from multiple state tokens by
program control flow, such as a branch/state-selection diamond. For the current sample
family it carries two branch outcomes, each with its own state token and target
original basic block. Deflattening rewrites conditional transitions when both
branch outcomes resolve, every path establishes its token, and the rewritten
region is private. It preserves arm execution when distinct dispatcher exits
exist. When both arms converge into one private semantic tail, it preserves the
tail and rewrites only its unique dispatcher exit using the already-written
state token. Otherwise it shortcuts the condition only with complete
state-channel bypass proof.

**Workflow phase**:
A named stage of per-function recovery work whose result controls whether later recovery work may run.

**Reanalysis-triggering mutation**:
A Binary Ninja function-state edit that can schedule function analysis again and therefore can re-enter the workflow.

**Phase cleanup**:
Dead target-decode IL removal for the indirect branch or call phase after its
owning workflow phase reaches stability. Its receipt is marked done only after
the current IL has no phase-owned cleanup changes left, so Binary Ninja
reanalysis can replay erased cleanup overlays. Deflatten state-write NOPs belong
to the atomic deflatten replacement, not phase cleanup. Branch condition
translation may contribute the exact contiguous assignment prefix of its proved
source IF; SSA liveness, rather than prefix membership alone, decides what is
dead.
