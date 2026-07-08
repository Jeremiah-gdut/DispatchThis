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
The narrow agreement a resolver profile must satisfy: recognize one binary's
indirect branch, indirect call, global constant, and string decrypt shapes, then
return standard recovery facts without owning workflow mutations. A profile may
implement a hook as a no-op when that binary does not use the capability.
Deflattening is not part of the contract.
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

**Profile helper**:
A reusable BNIL or BinaryView inspection helper used by resolver profiles and
passes to collect definitions, fold constants, read target data, validate
addresses, or build recovery facts. Profile helpers reduce per-binary resolver
code, but they do not own binary-specific recognition or workflow mutations.
_Avoid_: utils, generic rule engine, backend

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

**Recovery backend**:
The workflow or pass layer that consumes recovery facts and applies stable
Binary Ninja analysis effects, such as CFG recovery, call-target application, IL
translation, global slot typing, or cleanup. Resolver profiles and profile
helpers feed the backend; they do not replace it.
_Avoid_: profile helper, generic rule engine

**Indirect call resolving**:
Recovering the concrete callee of a computed call target.
_Avoid_: deincall

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
tokens. For current samples, identify it from equality comparisons whose
variables trace back to the same state variable; graph shape validates the
cluster but is not the primary signal.

**State variable**:
The value consumed by the dispatcher to select the next original block.

**State token**:
The opaque dispatcher value compared against the state variable. Its bit width is
part of its identity; do not assume all state tokens are 32-bit.

**Original basic block**:
A block from the original control flow before flattening redirected it through the dispatcher.
_Avoid_: OBB outside short code comments

**Deflattening**:
Reconnecting original basic blocks directly after dispatcher-controlled successors are recovered.

**Unconditional transition**:
A recovered original-block successor selected by a single state token write.

**Conditional transition**:
A recovered original-block successor set selected from multiple state tokens by
program control flow, such as a branch/state-selection diamond. For the current sample
family it carries two branch outcomes, each with its own state token and target
original basic block. Deflattening rewrites conditional transitions when both
branch outcomes resolve.

**Workflow phase**:
A named stage of per-function recovery work whose result controls whether later recovery work may run.

**Reanalysis-triggering mutation**:
A Binary Ninja function-state edit that can schedule function analysis again and therefore can re-enter the workflow.

**Phase cleanup**:
Dead IL removal that runs after its owning workflow phase reaches stability. Its receipt is marked done only after the current IL has no phase-owned cleanup changes left, so Binary Ninja reanalysis can replay erased cleanup overlays.
