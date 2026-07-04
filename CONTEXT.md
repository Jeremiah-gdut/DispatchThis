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

**Indirect call resolving**:
Recovering the concrete callee of a computed call target.
_Avoid_: deincall

**Global constant resolving**:
Recovering read-only semantics for global data slots that the sample family stores in writable sections but uses as constants.
_Avoid_: global variable fixing, data constant propagation

**Dispatcher**:
The flattened control-flow router that chooses the next original block from a state value.

**State variable**:
The value consumed by the dispatcher to select the next original block.

**Original basic block**:
A block from the original control flow before flattening redirected it through the dispatcher.
_Avoid_: OBB outside short code comments

**Deflattening**:
Reconnecting original basic blocks directly after dispatcher-controlled successors are recovered.

**Workflow phase**:
A named stage of per-function recovery work whose result controls whether later recovery work may run.

**Reanalysis-triggering mutation**:
A Binary Ninja function-state edit that can schedule function analysis again and therefore can re-enter the workflow.

**Phase cleanup**:
Dead IL removal that runs once after its owning workflow phase reaches stability, and runs again only if an upstream receipt change invalidates that phase.
