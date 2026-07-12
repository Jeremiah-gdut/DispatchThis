# ADR 0012: Call-target slices own dead-load cleanup

## Status

Accepted.

## Context

After an indirect call destination was replaced with a concrete callee, ordinary
phase cleanup removed the arithmetic decode assignments but deliberately retained
loads. HLIL therefore showed one unused global read per resolved call. Treating every
load as pure would make unrelated memory accesses removable. Proving immutability from
BinaryView xrefs is also unsuitable here because obfuscated or incomplete CFGs can make
xrefs incomplete.

## Decision

The indirect-call plan computes the complete current **SSA reaching-definition** slice
feeding `call.dest`. It follows exact `SSAVariable` versions and every `MLIL_VAR_PHI`
input. Only whole `MLIL_SET_VAR_SSA` definitions that map back to exact current
non-SSA `MLIL_SET_VAR` instructions become cleanup roots. Field, split, and aliased
definitions are proof boundaries. Assignments in the slice whose source contains a load
are additionally recorded as `cleanup_load_roots`.

At the mutation boundary the backend recomputes both root sets from the current call and
replaces any indices carried by the plan. If an exact SSA slice is unavailable, call
resolution may still proceed but cleanup receives no roots. After the call destination
is rewritten, phase cleanup uses current SSA uses to remove a witnessed load assignment
only when its value has no consumer outside the obsolete target computation. Calls,
stores, intrinsics, unimplemented IL, partial writes, and unrelated loads remain
ineligible. A call receipt or the contiguous assignments before its address never
reconstruct cleanup ownership. No xref participates in this ownership proof.

## Consequences

- Dead global reads left by indirect-call decoding disappear from HLIL.
- A target-decode value reused as a callback argument or by ordinary program logic stays
  live and is not removed.
- Profile-provided root indices cannot authorize cleanup after IL regeneration; current
  call-site SSA is the sole mutation-time authority.
- A profile-provided `decode_def` is descriptive evidence only. The rewrite changes the
  call destination, while the recomputed SSA slice exclusively owns decode cleanup.
- Call cleanup remains an MLIL overlay and may replay after Binary Ninja reanalysis.
