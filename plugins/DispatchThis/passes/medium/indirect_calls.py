"""MLIL-stage indirect-call resolver: rewrites decode-gadget call(reg) into call(const).

Folds the decode add (``target = (encoded + KEY) mod 2^48``) and rewrites the call
destination to a ``MLIL_CONST_PTR``. Also folds the spilled decode definition into
the same const so BN's dataflow can clean up the dead assignment.
"""

from ...helpers.facts import call_fact
from ...helpers.memory import is_call_target
from ...helpers.mlil import (
    CONST_OPS,
    cleanup_roots_for_expr,
    fold_constant_value,
    iter_indirect_calls,
    peel_var_definitions,
)
from ...utils.log import log_info, log_warn, log_error, log_debug


U48 = 0xFFFFFFFFFFFF
U64 = 0xFFFFFFFFFFFFFFFF

# --------------------------------------------------------------------------- #
# gadget parse + decode
# --------------------------------------------------------------------------- #

def resolve_call_target(bv, mlil, call_il):
    """Resolve the concrete target of one indirect call by folding its decode add.
    Returns ``(target, decode_def, cleanup_roots)`` or ``(None, None, set())``."""
    dest = call_il.dest

    # Already a direct call (const, or a var that folds to a const pointer).
    if dest.operation.name in CONST_OPS:
        return None, None, set()
    trail = []
    resolved = peel_var_definitions(mlil, dest, trail)
    if resolved.operation.name in CONST_OPS:
        return None, None, set()
    # A variant calls *through* the decoded slot (`call([rax + KEY])`). BN wraps
    # the decode add in an outer load, but the decoded pointer (the load's address
    # operand) is the real target -- not a dereference of it. Unwrap the load and
    # peel any further var indirection so the inner decode add hits the path below.
    if resolved.operation.name in ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT"):
        resolved = peel_var_definitions(mlil, resolved.src, trail)
        if resolved.operation.name in CONST_OPS:
            return None, None, set()
    if resolved.operation.name != "MLIL_ADD":
        log_debug(f"[icall] {hex(call_il.address)}: dest def is "
                  f"{resolved.operation.name}, not a decode add; skipping")
        return None, None, set()
    # The SET_VAR whose source is the decode add (last def walked), if any.
    decode_def = trail[-1] if trail else None

    # The key is the constant operand (the right per the gadget shape); the other
    # operand folds to the encoded target.
    left, right = resolved.left, resolved.right
    if right.operation.name in CONST_OPS:
        key_expr, enc_expr = right, left
    elif left.operation.name in CONST_OPS:
        key_expr, enc_expr = left, right
    else:
        # Key may sit behind a var that propagates a constant; default to the
        # right operand as the gadget always places the key there.
        key_expr, enc_expr = right, left

    key = fold_constant_value(bv, mlil, key_expr, load_address_mask=U48)
    if key is None:
        log_debug(f"[icall] {hex(call_il.address)}: could not fold decode key")
        return None, None, set()
    encoded = fold_constant_value(bv, mlil, enc_expr, load_address_mask=U48)
    if encoded is None:
        log_debug(f"[icall] {hex(call_il.address)}: could not fold encoded target")
        return None, None, set()

    cleanup_roots = cleanup_roots_for_expr(mlil, resolved)
    if decode_def is not None:
        cleanup_roots.add(decode_def.instr_index)

    # Modular decode wraps to the 48-bit address space; fall back to the full
    # 64-bit result if that does not land on a real callee.
    for mask in (U48, U64):
        target = (encoded + key) & mask
        if is_call_target(bv, target):
            return target, decode_def, cleanup_roots

    target = (encoded + key) & U48
    log_warn(f"[icall] {hex(call_il.address)}: (encoded {hex(encoded)} + key "
             f"{hex(key)}) = {hex(target)} is not a callee")
    return None, None, set()


# --------------------------------------------------------------------------- #
def plan_indirect_calls(bv, mlil):
    """Resolve decode-gadget indirect calls without mutating function state."""
    if mlil is None:
        log_warn("[icall] mlil is None")
        return []

    plans = []
    for call_il in iter_indirect_calls(mlil):
        try:
            target, decode_def, cleanup_roots = resolve_call_target(bv, mlil, call_il)
        except Exception as e:  # noqa: BLE001
            log_warn(f"[icall] {hex(call_il.address)}: {e}")
            continue
        if target is None:
            continue
        sym = bv.get_symbol_at(target)
        name = sym.name if sym else hex(target)
        log_info(f"[icall] {hex(call_il.address)}: indirect call -> "
                 f"{hex(target)} ({name})")
        plans.append(call_fact(
            call_il,
            target,
            decode_def=decode_def,
            cleanup_roots=cleanup_roots,
        ))

    if not plans:
        log_info("[icall] no indirect call gadgets resolved")
    return plans


def apply_indirect_call_rewrites(bv, mlil, plans):
    """Apply current-MLIL rewrites from indirect call plans."""
    if mlil is None or not plans:
        return 0
    addr_size = bv.arch.address_size
    applied = 0
    for plan in plans:
        call_il = plan["call_il"]
        target = plan["target"]
        decode_def = plan["decode_def"]
        # Replace the call destination with the resolved const pointer.
        try:
            mlil.replace_expr(
                call_il.dest.expr_index,
                mlil.const_pointer(addr_size, target),
            )
            applied += 1
            log_debug(f"[icall] {hex(call_il.address)} -> call {hex(target)}")
        except Exception as e:  # noqa: BLE001
            log_error(f"[icall] failed to rewrite {hex(call_il.address)}: {e}")
        # Fold the decode `var = encoded + key` that fed the call into the same
        # const pointer (`var = const`). We set it rather than NOP it: NOPing a
        # def whose variable still has uses leaves those uses dangling -- keeping
        # `var = const` lets BN's dataflow propagate the const and drop whatever
        # becomes dead on its own during finalize/SSA regeneration.
        if decode_def is not None:
            try:
                mlil.replace_expr(
                    decode_def.src.expr_index,
                    mlil.const_pointer(addr_size, target),
                )
                log_debug(f"[icall] folded decode def @ "
                          f"{hex(decode_def.address)} -> {hex(target)}")
            except Exception as e:  # noqa: BLE001
                log_error(f"[icall] failed to fold decode def @ "
                          f"{hex(decode_def.address)}: {e}")

    if applied:
        mlil.finalize()
        mlil.generate_ssa_form()
        log_info(f"[icall] {mlil.source_function.name}: rewrote {applied} indirect call(s)")
    return applied
