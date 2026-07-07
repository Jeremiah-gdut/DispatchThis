# Add profile helper primitives before a resolver framework

DispatchThis will reduce per-binary resolver profile development cost by adding
small profile helper modules for repeated BNIL and BinaryView inspection work:
LLIL definition walking, MLIL definition walking, constant folding, memory reads,
target validation, cleanup-root collection, and recovery-fact construction.

The helpers are shared building blocks for resolver profiles and passes. Resolver
profiles still own binary-specific recognition and target formulas, and workflow
callbacks still own Binary Ninja mutations, phase receipts, IL translation, and
cleanup application.

Profile helper APIs should be clear and documented because they may support
future profile plugins outside the bundled profile package. They do not need
compatibility shims before that external plugin surface exists; breaking helper
changes can be handled by updating the helper documentation and bundled profiles.
These APIs may expose Binary Ninja IL objects directly; do not introduce wrapper
types just to hide the Binary Ninja API.

The stable import surface is the helper package modules:
`DispatchThis.helpers.llil`, `DispatchThis.helpers.mlil`,
`DispatchThis.helpers.memory`, and `DispatchThis.helpers.facts`. Profiles should
prefer module imports such as `from DispatchThis.helpers import llil, facts` over
importing private helper implementation details.
The first helper pass should target this surface:

- `helpers.llil`: indirect-jump iteration, register definition peeling,
  `const_values`, PHI-aware constant candidates, and branch fact support.
- `helpers.mlil`: indirect-call iteration, variable definition peeling,
  single-value constant folding, expression walking, const-address extraction,
  and cleanup-root discovery.
- `helpers.memory`: explicit-width reads, target/address validation, section
  checks, and qword slot reads.
- `helpers.facts`: branch, call, global constant, and string decrypt recovery
  fact builders.

Build the first helper modules by moving reusable primitives out of existing
passes rather than wrapping the old pass-local functions. Helper functions should
own common edge-case handling such as missing IL, SSA/non-SSA mapping, unresolved
definitions, constants behind variables, invalid addresses, and cleanup roots
with live uses. Callers should not need to repeat those defensive checks.
PHI handling is part of that helper contract: LLIL constant helpers must account
for loop-carried PHI candidate values where possible, and MLIL cleanup-root
helpers must treat PHI nodes as slice/liveness connectors without turning the
PHI itself into a NOP target. CFG path or live-edge disambiguation belongs in a
profile or pass until a concrete shared scope proves otherwise.
For LLIL constant folding, prefer a multi-value API such as `const_values(...)`
that returns every concrete candidate as a set. Callers that require a single
key, base, or slot can check that the returned set has exactly one value instead
of using a separate single-value helper.
For MLIL call-target helpers, start with single-value constant folding because
the current call-target backend expects one concrete callee per call fact. Do
not design extra PHI or multi-candidate call-target behavior until a concrete
sample requires it.
Helpers may follow SSA definitions across basic blocks, including through
supported PHI handling, but they should not perform arbitrary CFG backward walks
or path enumeration in the first implementation. Prefer Binary Ninja's SSA
def-use information over rebuilding a second control-flow analysis.
Migrate existing passes to use the helpers, while keeping the `default` resolver
profile as a thin delegate to those passes. This keeps the current workflow
surface stable and ensures the helper API is exercised by production code rather
than documented as unused scaffolding.

Helper functions should treat failed recognition as data, not exceptions: shape
mismatches, unresolved constants, invalid targets, or absent candidate
instructions should return `None` or an empty collection. Reserve exceptions for
incorrect API use such as invalid argument types or malformed recovery facts.

Low-level helpers should not log normal recognition misses. Resolver profiles and
passes decide when a skipped site is meaningful enough to log. Helpers may raise
or return failure values, but they should not make broad candidate scans noisy.

Keep LLIL and MLIL helpers independent. `helpers.llil` must not depend on
`helpers.mlil`, and `helpers.mlil` must not depend on `helpers.llil`; shared
BinaryView or recovery-fact helpers belong in `helpers.memory` or
`helpers.facts`.

The first helper surface focuses on target recovery and cleanup-root collection,
not IL translation or generic IL rewriting. Branch translation, call target
application, deflattening rewrites, and cleanup application remain workflow/pass
backend responsibilities because those mutation sites are relatively stable once
profiles provide concrete targets and decode-garbage roots.
Branch condition translation belongs to the recovery backend: it rewrites stable
MLIL shapes after branch targets are known, and profile authors should not need
to customize that rewrite for each binary.

Global constant recovery may use profile helpers more directly. Common MLIL
walking, constant-address extraction, memory reads, store checks, and global
constant fact construction can move into helpers so profiles can express a
binary's slot rules without rewriting the underlying inspection code.
Do not move a high-level automatic global-constant planner into helpers yet.
Profiles and passes still decide which expressions are slot uses, which offset or
section rules apply, and which slots should become const facts.

Concrete targets and decode-garbage roots are both first-class recovery fact
information. Call facts already carry cleanup roots; branch facts should move in
the same direction so profiles can identify the instructions that computed a
target while workflow/pass backends decide when and how to clean them up.
Cleanup roots should be instruction-index sets. Binary Ninja distinguishes
instruction indices from expression indices: instruction indices identify
top-level IL instructions, while expression indices identify tree expressions and
are used for `replace_expr`. Profiles and helpers should return instruction
indices for cleanup roots; backend code can map SSA/non-SSA forms and use
expression indices only at the final replacement site.

Do not introduce a backward-slice class or dataclass in the first helper pass.
Return simple tuples, dicts, sets, and Binary Ninja IL objects until multiple
profiles prove that a dedicated slice object would remove real complexity.

Memory helpers should prefer explicit width and endianness. Provide helpers such
as `read_u8`, `read_u16le`, `read_u32le`, and `read_u64le`; any pointer helper
must take an explicit width or architecture/endian argument rather than hiding a
sample-specific pointer model.

Recovery-fact builders are recommended helpers, not a new contract layer. They
may reduce dict-field mistakes for common branch, call, global constant, and
string decrypt facts, but profile hooks may still return plain dict facts when a
special case is clearer.

Do not introduce a generic resolver engine, pattern DSL, or high-level
`resolve_all_*` framework yet. Those abstractions can wait until multiple binary
profiles prove the same higher-level resolver shape.
