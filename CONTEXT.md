# DispatchThis

DispatchThis is a Binary Ninja workflow plugin for recovering readable control flow from an ARM64 ELF obfuscation family.

## Language

**Sample family**:
A set of obfuscated ARM64 ELF binaries that share enough transformation patterns for one plugin profile to handle.
_Avoid_: generic target

**Decode gadget**:
A short obfuscation sequence that computes a control-flow or call target from encoded data at runtime.

**Indirect branch resolving**:
Recovering the concrete target of a computed jump so analysis can discover the next block.
_Avoid_: deinbr

**Resolver profile**:
A focused recognizer for one sample family's decode-gadget shape.
_Avoid_: generic rule engine

**Resolver profile contract**:
The narrow agreement a resolver profile must satisfy: recognize sample-family
specific indirect branch, indirect call, and global constant shapes, then return
standard recovery facts without owning workflow mutations. The first contract
requires hooks for all three capabilities, but a profile may implement a hook as
a no-op when its sample family does not use that capability. Deflattening is not
part of the first contract.
_Avoid_: middleware, adapter framework, plugin rewrite layer

**Active resolver profile**:
The resolver profile explicitly selected for a BinaryView. It chooses how enabled
functions interpret sample-family obfuscation shapes; it does not enable the
workflow for every function in the view.
_Avoid_: automatic sample detection

**Default resolver profile**:
The bundled resolver profile named `default`, representing the current sample-family
rules shipped with DispatchThis. The name does not mean generic support for every
obfuscation family.
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

**Indirect call resolving**:
Recovering the concrete callee of a computed call target.
_Avoid_: deincall

**Global constant resolving**:
Recovering read-only semantics for global data slots that the sample family stores in writable sections but uses as constants.
_Avoid_: global variable fixing, data constant propagation

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
program control flow, such as a branch/cmove diamond. For the current sample
family it carries two branch outcomes, each with its own state token and target
original basic block. Deflattening rewrites conditional transitions when both
branch outcomes resolve.

**Workflow phase**:
A named stage of per-function recovery work whose result controls whether later recovery work may run.

**Reanalysis-triggering mutation**:
A Binary Ninja function-state edit that can schedule function analysis again and therefore can re-enter the workflow.

**Phase cleanup**:
Dead IL removal that runs once after its owning workflow phase reaches stability, and runs again only if an upstream receipt change invalidates that phase.
