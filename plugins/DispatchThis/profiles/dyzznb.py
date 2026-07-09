from . import default
from ..helpers import facts, llil, memory, mlil
from ..utils.log import log_debug, log_error, log_info, log_warn


PROFILE_ID = "dyzznb"
PROFILE_NAME = "DYZZNB"
PROFILE_DESCRIPTION = "Rules for the dyzznb sample profile."

# Supported:
# - branch gadget: yes
# - indirect call gadget: yes
# - global constants: yes
# - deflatten: default planner
# - string decrypt: yes
#
# Validation:
# - branch/call: validated on sub_924464 and sub_8e09ac in libdyzznb.so
# - global constants/string decrypt: covered by fixture tests; sample workflow had no facts

U48 = llil.U48
U64 = 0xFFFFFFFFFFFFFFFF
CONST_SLOT_TYPE = "uint8_t const* const"

_CALL_OPS = ("MLIL_CALL", "MLIL_CALL_SSA", "MLIL_CALL_UNTYPED")
_CMP_OPS = (
    "MLIL_CMP_E",
    "MLIL_CMP_NE",
    "MLIL_CMP_ULT",
    "MLIL_CMP_SLT",
    "MLIL_CMP_ULE",
    "MLIL_CMP_SLE",
)
_MOD_OPS = ("MLIL_MODU", "MLIL_MODS", "MLIL_REMU", "MLIL_REMS")
_DIV_OPS = ("MLIL_DIVU", "MLIL_DIVS")


def _op(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def plan_deflatten_redirections(bv, func, il):
    return default.plan_deflatten_redirections(bv, func, il)


def _single_llil_const(bv, ssa, expr):
    values = llil.const_values(bv, ssa, expr)
    return next(iter(values)) if len(values) == 1 else None


def _qword_at(bv, addr):
    return memory.read_u64le(bv, addr & U48)


def _resolve_jump_addr(bv, slot, entry_offset, table_base_key, key):
    encoded_table_base = _qword_at(bv, slot)
    if encoded_table_base is None:
        return None
    table_base = (encoded_table_base + table_base_key) & U48
    encoded_target = _qword_at(bv, (table_base + entry_offset) & U48)
    if encoded_target is None:
        return None
    return (encoded_target + key) & U48


def _slot_from_load(bv, ssa, load_expr):
    if load_expr is None or _op(load_expr) not in llil.LOAD_OPS:
        return None
    src = load_expr.src
    if _op(src) in llil.CONST_OPS:
        return src.constant & U48
    return _single_llil_const(bv, ssa, src)


def _slot_add_const_candidates(bv, ssa, expr):
    expr = llil.peel_reg_definition(ssa, expr)
    if expr is None or _op(expr) != "LLIL_ADD":
        return []

    out = []
    for load_expr, const_expr in ((expr.left, expr.right), (expr.right, expr.left)):
        slot = _slot_from_load(bv, ssa, llil.peel_reg_definition(ssa, load_expr))
        value = _single_llil_const(bv, ssa, const_expr)
        if slot is not None and value is not None:
            out.append((slot, value & U48))
    return out


def _valid_offsets(bv, slot, table_base_key, key, offsets):
    out = set()
    for offset in offsets:
        target = _resolve_jump_addr(bv, slot, offset, table_base_key, key)
        if memory.is_valid_target(bv, target):
            out.add(offset & U48)
    return out


def _is_index_offset(ssa, expr):
    expr = llil.peel_reg_definition(ssa, expr)
    return expr is not None and _op(expr) in ("LLIL_LSL", "LLIL_LSR")


def _jump_parts(bv, ssa, jump_il):
    dest = jump_il.ssa_form.dest
    if _op(dest) != "LLIL_REG_SSA":
        return None

    final = llil.peel_reg_definition(ssa, dest)
    if final is None or _op(final) not in ("LLIL_ADD", "LLIL_SUB"):
        return None
    if _op(final) == "LLIL_SUB":
        chain_expr, key_expr, neg = final.left, final.right, True
    else:
        left = llil.peel_reg_definition(ssa, final.left)
        if left is not None and _op(left) in llil.LOAD_OPS:
            chain_expr, key_expr = final.left, final.right
        else:
            chain_expr, key_expr = final.right, final.left
        neg = False

    key = _single_llil_const(bv, ssa, key_expr)
    if key is None:
        return None
    if neg:
        key = -key

    load_expr = llil.peel_reg_definition(ssa, chain_expr)
    if load_expr is None or _op(load_expr) not in llil.LOAD_OPS or _op(load_expr.src) != "LLIL_ADD":
        return None
    addr = load_expr.src

    for table_base_expr, offset_expr in (
        (llil.peel_reg_definition(ssa, addr.left), addr.right),
        (llil.peel_reg_definition(ssa, addr.right), addr.left),
    ):
        if not _is_index_offset(ssa, offset_expr):
            continue
        for slot, table_base_key in _slot_add_const_candidates(bv, ssa, table_base_expr):
            offsets = {value & U48 for value in llil.const_values(bv, ssa, offset_expr)}
            offsets = _valid_offsets(bv, slot, table_base_key, key, offsets)
            if offsets:
                return slot, table_base_key, key & U48, offsets

    right = llil.peel_reg_definition(ssa, addr.right)
    if right is not None and _op(right) == "LLIL_ADD":
        offset_expr, table_base_expr = addr.left, right
    else:
        offset_expr = addr.right
        table_base_expr = llil.peel_reg_definition(ssa, addr.left)
    if table_base_expr is None or _op(table_base_expr) != "LLIL_ADD":
        return None

    table_base_key = _single_llil_const(bv, ssa, offset_expr)
    if table_base_key is None:
        return None

    left = llil.peel_reg_definition(ssa, table_base_expr.left)
    right = llil.peel_reg_definition(ssa, table_base_expr.right)
    if right is not None and _op(right) in llil.LOAD_OPS:
        slot_load, entry_offset_expr = right, table_base_expr.left
    else:
        slot_load, entry_offset_expr = left, table_base_expr.right
    if _op(slot_load) not in llil.LOAD_OPS:
        return None

    slot = _slot_from_load(bv, ssa, slot_load)
    offsets = llil.const_values(bv, ssa, entry_offset_expr)
    if slot is None or not offsets:
        return None
    return slot, table_base_key & U48, key & U48, {value & U48 for value in offsets}


def _resolve_llil_jump_targets(bv, ssa, jump_il):
    parsed = _jump_parts(bv, ssa, jump_il)
    if parsed is None:
        log_warn(f"[dyzznb:branch] shape mismatch @ {hex(jump_il.address)}")
        return []

    slot, table_base_key, key, offsets = parsed
    targets = []
    for offset in sorted(offsets):
        target = _resolve_jump_addr(bv, slot, offset, table_base_key, key)
        log_debug(
            f"[dyzznb:branch] {hex(jump_il.address)} slot={hex(slot)} "
            f"table_base_key={hex(table_base_key)} key={hex(key)} off={hex(offset)} -> "
            f"{hex(target) if target is not None else None}"
        )
        if target is not None:
            targets.append(target)
    return sorted(set(targets))


def resolve_branch_gadget(bv, il, known_targets=None):
    if not il:
        return []
    known_targets = known_targets or {}

    out = []
    ssa = il.ssa_form
    for jump_il in llil.iter_indirect_jumps(il):
        try:
            newly_resolved = jump_il.address not in known_targets
            if newly_resolved:
                targets = _resolve_llil_jump_targets(bv, ssa, jump_il)
            else:
                cached = known_targets[jump_il.address]
                targets = list(cached) if isinstance(cached, (list, tuple, set)) else [cached]
            targets = [target for target in targets if memory.is_valid_target(bv, target)]
            if targets:
                out.append(facts.branch_fact(
                    jump_il.address,
                    jump_il.dest.expr_index,
                    targets,
                    newly_resolved=newly_resolved,
                ))
        except Exception as exc:  # noqa: BLE001
            log_error(f"[dyzznb:branch] {hex(jump_il.address)}: {exc}")
    return out


def _resolve_call_target(bv, il, call_il):
    dest = call_il.dest
    if _op(dest) in mlil.CONST_OPS:
        return None, None, set()

    trail = []
    resolved = mlil.peel_var_definitions(il, dest, trail)
    if _op(resolved) in mlil.CONST_OPS:
        return None, None, set()
    if _op(resolved) in mlil.LOAD_OPS:
        resolved = mlil.peel_var_definitions(il, resolved.src, trail)
        if _op(resolved) in mlil.CONST_OPS:
            return None, None, set()
    if _op(resolved) != "MLIL_ADD":
        log_debug(f"[dyzznb:call] {hex(call_il.address)}: dest is {_op(resolved)}, not decode add")
        return None, None, set()

    decode_def = trail[-1] if trail else None
    left, right = resolved.left, resolved.right
    if _op(right) in mlil.CONST_OPS:
        key_expr, encoded_expr = right, left
    elif _op(left) in mlil.CONST_OPS:
        key_expr, encoded_expr = left, right
    else:
        key_expr, encoded_expr = right, left

    key = mlil.fold_constant_value(bv, il, key_expr, load_address_mask=U48)
    encoded = mlil.fold_constant_value(bv, il, encoded_expr, load_address_mask=U48)
    if key is None or encoded is None:
        log_debug(f"[dyzznb:call] {hex(call_il.address)}: could not fold target")
        return None, None, set()

    cleanup_roots = mlil.cleanup_roots_for_expr(il, resolved)
    if decode_def is not None:
        cleanup_roots.add(decode_def.instr_index)

    for mask in (U48, U64):
        target = (encoded + key) & mask
        if memory.is_call_target(bv, target):
            return target, decode_def, cleanup_roots

    target = (encoded + key) & U48
    log_warn(
        f"[dyzznb:call] {hex(call_il.address)}: encoded {hex(encoded)} + "
        f"key {hex(key)} -> {hex(target)} is not a callee"
    )
    return None, None, set()


def resolve_call_gadget(bv, il):
    if il is None:
        return []

    out = []
    for call_il in mlil.iter_indirect_calls(il):
        try:
            target, decode_def, cleanup_roots = _resolve_call_target(bv, il, call_il)
        except Exception as exc:  # noqa: BLE001
            log_warn(f"[dyzznb:call] {hex(call_il.address)}: {exc}")
            continue
        if target is None:
            continue
        sym = bv.get_symbol_at(target)
        name = sym.name if sym else hex(target)
        log_info(f"[dyzznb:call] {hex(call_il.address)}: indirect call -> {hex(target)} ({name})")
        out.append(facts.call_fact(
            call_il,
            target,
            decode_def=decode_def,
            cleanup_roots=cleanup_roots,
        ))
    return out


def _plain_ptr_var(bv, addr):
    data_var = bv.get_data_var_at(addr)
    return data_var is not None and str(data_var.type).replace(" ", "") == "void*"


def _ref_functions(bv, data_var):
    seen = set()
    for ref in list(getattr(data_var, "code_refs", ()) or ()):
        func = getattr(ref, "function", None)
        funcs = [func] if func is not None else []
        if func is None:
            try:
                funcs = list(bv.get_functions_containing(ref.address))
            except Exception:  # noqa: BLE001
                funcs = []
        for func in funcs:
            key = getattr(func, "start", id(func))
            if key not in seen:
                seen.add(key)
                yield func


def _refs_store_slot(bv, current_il, slot_addr):
    data_var = bv.get_data_var_at(slot_addr)
    if mlil.mlil_stores_to_address(current_il, slot_addr, address_mask=U48):
        return True
    for func in _ref_functions(bv, data_var):
        ref_il = getattr(func, "mlil", None)
        if ref_il is not None and ref_il is not current_il:
            if mlil.mlil_stores_to_address(ref_il, slot_addr, address_mask=U48):
                return True
    return False


def _global_plan_for_slot(bv, il, slot_addr, offset, use_addr):
    if offset == 0:
        return None
    if not _plain_ptr_var(bv, slot_addr) or not memory.in_section(bv, slot_addr, ".data"):
        return None
    value = memory.read_qword_slot(bv, slot_addr)
    if value is None:
        return None
    resolved_addr = (value + offset) & U48
    if not memory.is_valid_target(bv, resolved_addr):
        return None
    if _refs_store_slot(bv, il, slot_addr):
        log_warn(f"[dyzznb:gconst] {hex(slot_addr)}: skipped, known reference writes to slot")
        return None
    return facts.global_constant_fact(slot_addr, CONST_SLOT_TYPE, value, resolved_addr, use_addr)


def plan_global_constant_slots(bv, il):
    if il is None:
        return []

    plans = {}
    for _expr, use_addr, slot_addr, offset in mlil.iter_load_slot_offsets(il, address_mask=U48):
        if slot_addr in plans:
            continue
        plan = _global_plan_for_slot(bv, il, slot_addr, offset, use_addr)
        if plan is not None:
            plans[slot_addr] = plan
    return [plans[addr] for addr in sorted(plans)]


def _const(il, expr):
    return mlil.expression_scalar_value(il, expr)


def _has_const(expr, value):
    return any(_op(child) in mlil.CONST_OPS and child.constant == value for child in mlil.walk_expr(expr))


def _has_remainder_index(il, expr, key_modulus):
    expanded = list(mlil.walk_expr_with_defs(il, expr))
    if any(_op(child) in _MOD_OPS and _has_const(child, key_modulus) for child in expanded):
        return True
    return (
        any(_op(child) == "MLIL_SUB" for child in expanded)
        and any(_op(child) in _DIV_OPS and _has_const(child, key_modulus) for child in expanded)
        and any(_op(child) == "MLIL_MUL" and _has_const(child, key_modulus) for child in expanded)
    )


def _key_modulus_constants(il):
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            op = _op(expr)
            if op not in (*_MOD_OPS, *_DIV_OPS):
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                value = _const(il, side)
                if value and (0 if op in _MOD_OPS else 1) < value <= 256:
                    out.add(value)
    return sorted(out)


def _length_constants(il, key_modulus):
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _CMP_OPS:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                value = _const(il, side)
                if value and value != key_modulus and 0 < value <= 4096:
                    out.add(value)
    return sorted(out)


def _parameters(func, il):
    for owner in (func, getattr(il, "source_function", None)):
        params = getattr(owner, "parameter_vars", None)
        if params:
            return list(params)
    return []


def _has_sample_family_loads(il, key_modulus):
    byte_loads = 0
    key_load = False
    for ins in getattr(il, "instructions", ()) or ():
        for load in (expr for expr in mlil.walk_expr(ins) if _op(expr) in mlil.LOAD_OPS):
            if getattr(load, "size", None) != 1:
                continue
            byte_loads += 1
            key_load = _has_remainder_index(il, getattr(load, "src", None), key_modulus) or key_load
    return key_load and byte_loads >= 2


def _has_byte_write_from_xor(il):
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in mlil.STORE_OPS or getattr(expr, "size", None) != 1:
                continue
            if any(_op(child) == "MLIL_XOR" for child in mlil.walk_expr_with_defs(il, getattr(expr, "src", None))):
                return True
    return False


def _has_done_flag_store(il):
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) in mlil.STORE_OPS and _const(il, getattr(expr, "src", None)) == 1:
                return True
    return False


def _recognize_string_decrypt_function(func, il=None):
    il = il or getattr(func, "mlil", None) or getattr(func, "medium_level_il", None)
    if il is None or len(_parameters(func, il)) < 2:
        return None
    for key_modulus in _key_modulus_constants(il):
        lengths = _length_constants(il, key_modulus)
        if (
            lengths
            and _has_sample_family_loads(il, key_modulus)
            and _has_byte_write_from_xor(il)
            and _has_done_flag_store(il)
        ):
            return {"key_modulus": key_modulus, "length": lengths[-1]}
    return None


def _decode_string_blob(bv, source_addr, spec):
    key_modulus = spec["key_modulus"]
    length = spec["length"]
    try:
        data = bv.read(source_addr, key_modulus + length)
    except Exception:  # noqa: BLE001
        return None
    if data is None or len(data) < key_modulus + length:
        return None

    key = data[:key_modulus]
    payload = data[key_modulus:key_modulus + length]
    out = bytearray()
    prev = 0
    for index, encoded in enumerate(payload):
        k = key[index % key_modulus]
        if (((index % key_modulus) * k) & 1) == 0:
            tmp = ((prev + encoded) & 0xFF) ^ ((~k) & 0xFF)
        else:
            tmp = (-(((encoded - prev) & 0xFF) ^ k)) & 0xFF
        ch = tmp ^ k
        out.append(ch)
        prev = ch
    return bytes(out)


def plan_string_decrypt_calls(bv, _func, il, mlil_stable):
    if il is None:
        return []
    mlil_stable = mlil_stable or {}

    out = []
    for call in mlil.iter_calls(il, _CALL_OPS):
        target = _const(il, getattr(call, "dest", None))
        params = list(getattr(call, "params", ()) or ())
        if target is None or len(params) < 2:
            continue
        dst_addr = _const(il, params[0])
        src_addr = _const(il, params[1])
        if dst_addr is None or src_addr is None:
            continue
        callee = bv.get_function_at(target)
        if callee is None or not mlil_stable.get(getattr(callee, "start", None)):
            continue
        spec = _recognize_string_decrypt_function(callee)
        if spec is None:
            continue
        plaintext = _decode_string_blob(bv, src_addr, spec)
        if plaintext is None:
            log_warn(f"[dyzznb:sdecrypt] {hex(call.address)}: source blob @ {hex(src_addr)} is too short")
            continue
        out.append(facts.string_decrypt_fact(call.address, src_addr, dst_addr, plaintext))
    return out
