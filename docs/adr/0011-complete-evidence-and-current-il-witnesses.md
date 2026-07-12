# Require complete evidence and current IL witnesses

DispatchThis will publish recovery facts only from complete evidence. A value
folder returns every value supported by every semantic path, or `None` when any
path is unknown. A bounded expansion, unreadable load, unsupported operation,
ambiguous PHI relation, or invalid member of a recovered target set is not
permission to keep the remaining members.

The PHI-correlation seam has a separate control result: `None` means that no
multi-PHI relation was detected and permits the ordinary complete-value folder,
while an empty set means a relation was detected but could not be proved and
forbids fallback. That empty set is a rejection sentinel, not a recovered value
set or permission to keep an uncorrelated subset.

Consumers that require one value must first receive a complete set and then
check that its cardinality is one. Helpers and profiles must not expose
"first", "best", or valid-subset conveniences for branch or call targets.
When several witnesses describe one site, all witnesses must agree on the same
semantics before a fact is published.

Recovery plans that later rewrite IL must retain their Binary Ninja instruction
witnesses. At the mutation boundary, the backend maps each witness to the
current `AnalysisContext` IL and verifies its instruction index, expression
index, operation, address, relevant operands, and owning IL function. A stale or
malformed witness rejects the plan atomically; it is never recovered by scanning
for a similar instruction elsewhere in the function.

Binary Ninja's native operation enums are the implementation vocabulary for
single-IL modules. Exported operation-name tuples remain only at the deliberate
mixed-LLIL/MLIL compatibility seam, where equal `IntEnum` values could otherwise
confuse the IL level. Those names are generated from Binary Ninja enums rather
than hand-written.

Workflow receipts are coordination state, not analysis truth. Branch metadata,
call type adjustments, global data-variable types, and current IL witnesses are
read back from Binary Ninja before a receipt is treated as satisfied. Function
phase state is also bound to its resolver profile ID; state with recovery
evidence cannot be reused under a different profile.

This decision favors a missed optimization over an incorrect CFG edge, call
prototype, state transition, or cleanup NOP. Support for a new obfuscation shape
should add a complete proof for that shape rather than weakening these mutation
boundaries.
