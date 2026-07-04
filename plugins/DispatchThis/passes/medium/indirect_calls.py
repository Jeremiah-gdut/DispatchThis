"""MLIL-stage indirect-call resolver: rewrites decode-gadget call(reg) into call(const).

Folds the decode add (``target = (encoded + KEY) mod 2^48``) and rewrites the call
destination to a ``MLIL_CONST_PTR``. Also folds the spilled decode definition into
the same const so BN's dataflow can clean up the dead assignment.
"""

from ...utils.log import log_info, log_warn, log_error, log_debug


U64 = 0xFFFFFFFFFFFFFFFF
U48 = 0xFFFFFFFFFFFF

_CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")


# --------------------------------------------------------------------------- #
# constant recovery
# --------------------------------------------------------------------------- #

def get_qword_at(bv, addr):
    """Read a little-endian qword from image memory. ``None`` if unmapped."""
    data = bv.read(addr & U48, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _peel_def(mlil, expr, trail=None):
    """Follow ``MLIL_VAR`` through ``SET_VAR`` definitions to the underlying expression.
    Appends each walked ``SET_VAR`` to ``trail`` if provided."""
    for _ in range(64):
        if expr is None or expr.operation.name != "MLIL_VAR":
            break
        defs = mlil.get_var_definitions(expr.src)
        if not defs or defs[0].operation.name != "MLIL_SET_VAR":
            break
        if trail is not None:
            trail.append(defs[0])
        expr = defs[0].src
    return expr


def eval_const(bv, mlil, il, depth=0):
    """Best-effort constant-fold an MLIL expression; returns ``int`` or ``None``.

    Folds the call-gadget decode (``encoded + key`` where ``encoded`` is loaded
    from image memory and ``key`` is a propagated constant). Definitions are
    followed first, with BN value-set analysis as a fallback for the registers
    the prologue seeds with the key constant.
    """
    if il is None or depth > 32:
        return None
    op = il.operation.name

    if op in _CONST_OPS:
        return il.constant & U64

    if op == "MLIL_VAR":
        # Fold through the definition first: it reads current memory and the
        # gadget arithmetic. BN's value-set can mis-resolve the relocated-pointer
        # gadgets, so only fall back to it when no definition folds.
        defs = mlil.get_var_definitions(il.src)
        if defs and defs[0].operation.name == "MLIL_SET_VAR":
            r = eval_const(bv, mlil, defs[0].src, depth + 1)
            if r is not None:
                return r
        v = il.value
        if v.type.name in ("ConstantValue", "ConstantPointerValue",
                           "ImportedAddressValue"):
            return v.value & U64
        return None

    if op in ("MLIL_ADD", "MLIL_SUB"):
        l = eval_const(bv, mlil, il.left, depth + 1)
        r = eval_const(bv, mlil, il.right, depth + 1)
        if l is None or r is None:
            return None
        return (l + r if op == "MLIL_ADD" else l - r) & U64

    if op == "MLIL_MUL":
        l = eval_const(bv, mlil, il.left, depth + 1)
        r = eval_const(bv, mlil, il.right, depth + 1)
        return None if l is None or r is None else (l * r) & U64

    if op in ("MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"):
        return eval_const(bv, mlil, il.src, depth + 1)

    if op in ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT"):
        addr = eval_const(bv, mlil, il.src, depth + 1)
        if addr is None:
            return None
        data = bv.read(addr & U48, il.size)     # only reads image memory
        if len(data) < il.size:                 # stack / invalid -> give up
            return None
        return int.from_bytes(data, "little")

    # Last resort: BN dataflow over the whole expression.
    v = il.value
    if v.type.name in ("ConstantValue", "ConstantPointerValue",
                       "ImportedAddressValue"):
        return v.value & U64
    return None


def _is_callee(bv, addr):
    """Does ``addr`` look like a real call target (import thunk / function)?"""
    return bv.is_valid_offset(addr) and (
        bv.get_symbol_at(addr) is not None or bv.get_function_at(addr) is not None)


# --------------------------------------------------------------------------- #
# gadget parse + decode
# --------------------------------------------------------------------------- #

def resolve_call_target(bv, mlil, call_il):
    """Resolve the concrete target of one indirect call by folding its decode add.
    Returns ``(target, decode_def)`` or ``(None, None)``."""
    dest = call_il.dest

    # Already a direct call (const, or a var that folds to a const pointer).
    if dest.operation.name in _CONST_OPS:
        return None, None
    trail = []
    resolved = _peel_def(mlil, dest, trail)
    if resolved.operation.name in _CONST_OPS:
        return None, None
    # A variant calls *through* the decoded slot (`call([rax + KEY])`). BN wraps
    # the decode add in an outer load, but the decoded pointer (the load's address
    # operand) is the real target -- not a dereference of it. Unwrap the load and
    # peel any further var indirection so the inner decode add hits the path below.
    if resolved.operation.name in ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT"):
        resolved = _peel_def(mlil, resolved.src, trail)
        if resolved.operation.name in _CONST_OPS:
            return None, None
    if resolved.operation.name != "MLIL_ADD":
        log_debug(f"[icall] {hex(call_il.address)}: dest def is "
                  f"{resolved.operation.name}, not a decode add; skipping")
        return None, None
    # The SET_VAR whose source is the decode add (last def walked), if any.
    decode_def = trail[-1] if trail else None

    # The key is the constant operand (the right per the gadget shape); the other
    # operand folds to the encoded target.
    left, right = resolved.left, resolved.right
    if right.operation.name in _CONST_OPS:
        key_expr, enc_expr = right, left
    elif left.operation.name in _CONST_OPS:
        key_expr, enc_expr = left, right
    else:
        # Key may sit behind a var that propagates a constant; default to the
        # right operand as the gadget always places the key there.
        key_expr, enc_expr = right, left

    key = eval_const(bv, mlil, key_expr)
    if key is None:
        log_debug(f"[icall] {hex(call_il.address)}: could not fold decode key")
        return None, None
    encoded = eval_const(bv, mlil, enc_expr)
    if encoded is None:
        log_debug(f"[icall] {hex(call_il.address)}: could not fold encoded target")
        return None, None

    # Modular decode wraps to the 48-bit address space; fall back to the full
    # 64-bit result if that does not land on a real callee.
    for mask in (U48, U64):
        target = (encoded + key) & mask
        if _is_callee(bv, target):
            return target, decode_def

    target = (encoded + key) & U48
    log_warn(f"[icall] {hex(call_il.address)}: (encoded {hex(encoded)} + key "
             f"{hex(key)}) = {hex(target)} is not a callee")
    return None, None


# --------------------------------------------------------------------------- #
# pass entry
# --------------------------------------------------------------------------- #

def iter_indirect_calls(mlil):
    """Yield every ``MLIL_CALL*`` whose destination is not already a const."""
    for insn in mlil.instructions:
        if not insn.operation.name.startswith("MLIL_CALL"):
            continue
        if insn.dest.operation.name in _CONST_OPS:
            continue                              # already a direct call
        yield insn


def plan_indirect_calls(bv, mlil):
    """Resolve decode-gadget indirect calls without mutating function state."""
    if mlil is None:
        log_warn("[icall] mlil is None")
        return []

    plans = []
    for call_il in iter_indirect_calls(mlil):
        try:
            target, decode_def = resolve_call_target(bv, mlil, call_il)
        except Exception as e:  # noqa: BLE001
            log_warn(f"[icall] {hex(call_il.address)}: {e}")
            continue
        if target is None:
            continue
        sym = bv.get_symbol_at(target)
        name = sym.name if sym else hex(target)
        log_info(f"[icall] {hex(call_il.address)}: indirect call -> "
                 f"{hex(target)} ({name})")
        plans.append({
            "call_il": call_il,
            "call_addr": call_il.address,
            "target": target,
            "decode_def": decode_def,
        })

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


def patch_indirect_calls(bv, mlil):
    """Resolve and rewrite indirect calls in the current MLIL only."""
    return apply_indirect_call_rewrites(bv, mlil, plan_indirect_calls(bv, mlil))
