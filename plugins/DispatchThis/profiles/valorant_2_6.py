from ..helpers import facts, llil, memory, mlil
from ..utils.log import log_warn


PROFILE_ID = "valorant_2_6"
PROFILE_NAME = "Valorant 2.6"
PROFILE_DESCRIPTION = (
    "Rules for the valorant_2_6 binary: main branch and call gadgets; "
    "global constants; no-op strings."
)

# Supported:
# - branch gadget: yes
# - indirect call gadget: yes
# - global constants: yes
# - string decrypt: no-op
#
# Validation:
# - branch: 0x6c5f6c -> 0x6c5f70, 0x6c6a4c
# - call: 0x6c5ee0 -> 0x6cb194

U48 = llil.U48
U64 = 0xFFFFFFFFFFFFFFFF
MAIN_BRANCH_KEY = 0x5C76880DE50178C9
CONST_SLOT_TYPE = "void const* const"
_CONST_DATA_SECTIONS = {".data", ".rodata"}
_GLOBAL_CONSTANT_SLOT_RANGE = range(0x12A01E0, 0x12A0E38, 8)
_SCALAR_CONSTANT_BLOB_RANGE = (0x11F5700, 0x11F5878)
_SCALAR_CONST_TYPES = {
    1: "uint8_t const",
    2: "uint16_t const",
    4: "uint32_t const",
    8: "uint64_t const",
}
_SCALAR_CONST_WIDTHS = {type_name: width for width, type_name in _SCALAR_CONST_TYPES.items()}

_CONST_OPS = (*llil.CONST_OPS, *mlil.CONST_OPS)
_LOAD_OPS = (*llil.LOAD_OPS, *mlil.LOAD_OPS)
_SET_OPS = (*llil.SET_REG_OPS, *mlil.SET_VAR_OPS, "MLIL_SET_VAR_SSA")
_PHI_OPS = ("LLIL_REG_PHI", "MLIL_VAR_PHI")


def _iter_scalar_constant_loads(bv, il):
    start, end = _SCALAR_CONSTANT_BLOB_RANGE
    for ins in getattr(il, "instructions", ()) or ():
        ins_addr = getattr(ins, "address", 0)
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _LOAD_OPS:
                continue
            width = getattr(expr, "size", 0)
            if width not in _SCALAR_CONST_TYPES:
                continue
            offset = getattr(expr, "offset", 0) or 0
            for addr in _values(bv, il, expr.src):
                addr = ((addr + offset) & U48)
                if start <= addr and addr + width <= end:
                    yield addr, width, getattr(expr, "address", ins_addr)


def _add_global_constant_plan(plans, bv, slot_addr, type_name):
    if slot_addr in plans:
        return
    if not memory.in_section(bv, slot_addr, _CONST_DATA_SECTIONS):
        return
    value = memory.read_qword_slot(bv, slot_addr)
    if value is None:
        return
    plans[slot_addr] = facts.global_constant_fact(
        slot_addr,
        type_name,
        value,
        value & U48,
        0,
    )


def _add_scalar_constant_plan(plans, bv, slot_addr, width, use_addr):
    type_name = _SCALAR_CONST_TYPES.get(width)
    if type_name is None or not memory.in_section(bv, slot_addr, _CONST_DATA_SECTIONS):
        return
    existing = plans.get(slot_addr)
    if existing is not None and width <= _SCALAR_CONST_WIDTHS.get(existing.get("type"), 0):
        return
    value = memory.read_uint_le(bv, slot_addr, width)
    if value is None:
        return
    plans[slot_addr] = facts.global_constant_fact(
        slot_addr,
        type_name,
        value,
        value & U48,
        use_addr,
    )


def _op(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def _definition(il, var):
    getter = getattr(il, "get_ssa_reg_definition", None)
    if getter is not None:
        try:
            return getter(var)
        except Exception:  # noqa: BLE001
            return None
    return None


def _ssa_var_definition(il, var):
    getter = getattr(il, "get_ssa_var_definition", None)
    if getter is not None:
        try:
            return getter(var)
        except Exception:  # noqa: BLE001
            return None
    return None


def _var_definitions(il, var):
    getter = getattr(il, "get_var_definitions", None)
    if getter is None:
        return ()
    try:
        return tuple(getter(var) or ())
    except Exception:  # noqa: BLE001
        return ()


def _stack_slot(expr):
    if expr is None or _op(expr) not in ("LLIL_ADD", "LLIL_SUB"):
        return None
    if _op(expr) == "LLIL_ADD":
        pairs = ((expr.left, expr.right), (expr.right, expr.left))
    else:
        pairs = ((expr.left, expr.right),)
    for reg_expr, const_expr in pairs:
        if _op(reg_expr) != "LLIL_REG_SSA" or _op(const_expr) not in llil.CONST_OPS:
            continue
        reg = getattr(reg_expr.src, "reg", None)
        if str(reg) in ("sp", "fp"):
            value = const_expr.constant if _op(expr) == "LLIL_ADD" else -const_expr.constant
            return str(reg_expr.src), value
    return None


def _stack_store_source(ssa, load_expr):
    slot = _stack_slot(getattr(load_expr, "src", None))
    if ssa is None or slot is None:
        return None
    best = None
    best_index = -1
    load_index = getattr(load_expr, "instr_index", 1 << 60)
    try:
        blocks = iter(ssa)
    except TypeError:
        return None
    for block in blocks:
        for insn in block:
            if _op(insn) not in ("LLIL_STORE", "LLIL_STORE_SSA"):
                continue
            instr_index = getattr(insn, "instr_index", -1)
            if instr_index >= load_index or instr_index <= best_index:
                continue
            if _stack_slot(insn.dest) == slot:
                best = insn.src
                best_index = instr_index
    return best


def _values_for_phi_operand(bv, il, operand, depth, max_depth, seen, bindings=None):
    if hasattr(operand, "operation"):
        return _values(bv, il, operand, depth, max_depth, seen, bindings)
    definition = _definition(il, operand) or _ssa_var_definition(il, operand)
    if definition is not None:
        return _values(bv, il, definition, depth, max_depth, seen, bindings)
    out = set()
    for definition in _var_definitions(il, operand):
        out.update(_values(bv, il, definition, depth, max_depth, seen.copy(), bindings))
    return out


def _bound_value(bindings, var):
    if not bindings:
        return None
    if var in bindings:
        return bindings[var]
    return bindings.get(str(var))


def _values(bv, il, expr, depth=0, max_depth=64, seen=None, bindings=None):
    if expr is None or depth > max_depth:
        return set()
    if seen is None:
        seen = set()
    key = (_op(expr), getattr(expr, "expr_index", None), getattr(expr, "instr_index", None), str(expr))
    if key in seen:
        return set()
    seen.add(key)

    op = _op(expr)
    if op in _CONST_OPS:
        return {expr.constant & U64}

    if op in ("LLIL_ZX", "LLIL_SX", "LLIL_LOW_PART", "MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"):
        return _values(bv, il, expr.src, depth + 1, max_depth, seen, bindings)

    if op in ("LLIL_REG_SSA", "LLIL_REG"):
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & U64}
        definition = _definition(il, expr.src)
        return set() if definition is None else _values(bv, il, definition, depth + 1, max_depth, seen, bindings)

    if op == "MLIL_VAR_SSA":
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & U64}
        definition = _ssa_var_definition(il, expr.src)
        return set() if definition is None else _values(bv, il, definition, depth + 1, max_depth, seen, bindings)

    if op == "MLIL_VAR":
        out = set()
        for definition in _var_definitions(il, expr.src):
            out.update(_values(bv, il, definition, depth + 1, max_depth, seen.copy(), bindings))
        return out

    if op in _SET_OPS:
        return _values(bv, il, expr.src, depth + 1, max_depth, seen, bindings)

    if op in _PHI_OPS:
        out = set()
        for operand in getattr(expr, "src", ()) or ():
            out.update(_values_for_phi_operand(bv, il, operand, depth + 1, max_depth, seen.copy(), bindings))
        return out

    if op in _LOAD_OPS:
        stack_src = _stack_store_source(il, expr)
        if stack_src is not None:
            return _values(bv, il, stack_src, depth + 1, max_depth, seen, bindings)
        out = set()
        size = getattr(expr, "size", 8)
        for addr in _values(bv, il, expr.src, depth + 1, max_depth, seen.copy(), bindings):
            value = memory.read_uint_le(bv, addr & U48, size)
            if value is not None:
                out.add(value & U64)
        return out

    if op in ("LLIL_NEG", "MLIL_NEG"):
        return {(-value) & U64 for value in _values(bv, il, expr.src, depth + 1, max_depth, seen, bindings)}

    if op in (
        "LLIL_ADD",
        "MLIL_ADD",
        "LLIL_SUB",
        "MLIL_SUB",
        "LLIL_MUL",
        "MLIL_MUL",
        "LLIL_AND",
        "MLIL_AND",
        "LLIL_OR",
        "MLIL_OR",
        "LLIL_XOR",
        "MLIL_XOR",
        "LLIL_LSL",
        "MLIL_LSL",
        "LLIL_LSR",
        "MLIL_LSR",
    ):
        lefts = _values(bv, il, expr.left, depth + 1, max_depth, seen.copy(), bindings)
        rights = _values(bv, il, expr.right, depth + 1, max_depth, seen.copy(), bindings)
        out = set()
        for left in lefts:
            for right in rights:
                if op.endswith("_ADD"):
                    out.add((left + right) & U64)
                elif op.endswith("_SUB"):
                    out.add((left - right) & U64)
                elif op.endswith("_MUL"):
                    out.add((left * right) & U64)
                elif op.endswith("_AND"):
                    out.add((left & right) & U64)
                elif op.endswith("_OR"):
                    out.add((left | right) & U64)
                elif op.endswith("_XOR"):
                    out.add((left ^ right) & U64)
                elif op.endswith("_LSL"):
                    out.add((left << right) & U64)
                elif op.endswith("_LSR"):
                    out.add((left >> right) & U64)
        return out

    return set()


def _collect_phi_regs(il, expr, out=None, seen=None):
    if out is None:
        out = set()
    if seen is None:
        seen = set()
    key = (_op(expr), getattr(expr, "expr_index", None), getattr(expr, "instr_index", None), str(expr))
    if expr is None or key in seen:
        return out
    seen.add(key)

    op = _op(expr)
    if op in ("LLIL_REG_SSA", "LLIL_REG"):
        definition = _definition(il, expr.src)
        if _op(definition) == "LLIL_REG_PHI":
            out.add(expr.src)
        _collect_phi_regs(il, definition, out, seen)
        return out
    if op in _SET_OPS:
        _collect_phi_regs(il, expr.src, out, seen)
        return out
    for name in ("src", "left", "right", "dest", "condition"):
        child = getattr(expr, name, None)
        if hasattr(child, "operation"):
            _collect_phi_regs(il, child, out, seen)
    return out


def _branch_values(bv, ssa, dest):
    correlated = llil.correlated_phi_values(
        ssa,
        dest,
        lambda operand, bindings=None: _values_for_phi_operand(
            bv,
            ssa,
            operand,
            0,
            64,
            set(),
            bindings,
        ),
        max_depth=64,
    )
    if correlated is not None:
        return correlated
    phi_regs = tuple(_collect_phi_regs(ssa, dest))
    if len(phi_regs) != 1:
        return _values(bv, ssa, dest)

    out = set()
    phi_reg = phi_regs[0]
    for value in _values(bv, ssa, _definition(ssa, phi_reg)):
        bindings = {phi_reg: value, str(phi_reg): value}
        out.update(_values(bv, ssa, dest, bindings=bindings))
    return out


def _candidate_targets(values):
    out = set()
    for value in values:
        out.add(value & U64)
        out.add(value & U48)
    return tuple(sorted(out))


def _jump_dest(jump_il):
    return getattr(getattr(jump_il, "ssa_form", None), "dest", None) or getattr(jump_il, "dest", None)


def _valid_branch_target(bv, target):
    return (
        target % 4 == 0
        and memory.is_valid_target(bv, target)
        and memory.in_section(bv, target, ".text")
    )


def resolve_branch_gadget(bv, il, known_targets=None):
    if not il:
        return []
    known_targets = known_targets or {}
    ssa = getattr(il, "ssa_form", il)
    out = []
    for jump_il in llil.iter_indirect_jumps(il):
        newly_resolved = jump_il.address not in known_targets
        targets = _candidate_targets(_branch_values(bv, ssa, _jump_dest(jump_il)))
        if not targets and not newly_resolved:
            cached = known_targets[jump_il.address]
            targets = tuple(cached) if isinstance(cached, (list, tuple, set)) else (cached,)
        targets = [target for target in targets if _valid_branch_target(bv, target)]
        if targets:
            out.append(facts.branch_fact(
                jump_il.address,
                jump_il.dest.expr_index,
                targets,
                newly_resolved=newly_resolved,
            ))
    return out


def _single_decode_def(il, dest):
    if _op(dest) not in ("MLIL_VAR", "MLIL_VAR_SSA"):
        return None
    defs = [definition for definition in _var_definitions(il, dest.src) if _op(definition) in _SET_OPS]
    return defs[0] if len(defs) == 1 else None


def _valid_call_target(bv, target):
    try:
        if bv.get_symbol_at(target) is not None:
            return True
    except Exception:  # noqa: BLE001
        pass
    return memory.is_call_target(bv, target) or (
        memory.is_valid_target(bv, target) and memory.in_section(bv, target, ".text")
    )


def _call_dest_values(bv, il, call_il):
    ssa = getattr(il, "ssa_form", None)
    ssa_dest = getattr(getattr(call_il, "ssa_form", None), "dest", None)
    if ssa is not None and ssa_dest is not None:
        values = _values(bv, ssa, ssa_dest)
        if values:
            return values
    return _values(bv, il, call_il.dest)


def resolve_call_gadget(bv, il):
    if il is None:
        return []

    out = []
    for call_il in mlil.iter_indirect_calls(il):
        targets = [
            target
            for target in _candidate_targets(_call_dest_values(bv, il, call_il))
            if _valid_call_target(bv, target)
        ]
        if len(targets) != 1:
            if targets:
                log_warn(f"[valorant_2_6:call] {hex(call_il.address)}: multiple targets")
            continue
        decode_def = _single_decode_def(il, call_il.dest)
        cleanup_roots = mlil.cleanup_roots_for_expr(il, call_il.dest)
        if decode_def is not None:
            cleanup_roots.add(decode_def.instr_index)
        out.append(facts.call_fact(
            call_il,
            targets[0],
            decode_def=decode_def,
            cleanup_roots=cleanup_roots,
        ))
    return out


def plan_global_constant_slots(bv, il):
    if il is None:
        return []

    plans = {}
    for slot_addr in _GLOBAL_CONSTANT_SLOT_RANGE:
        _add_global_constant_plan(plans, bv, slot_addr, CONST_SLOT_TYPE)
    for slot_addr, width, use_addr in _iter_scalar_constant_loads(bv, il):
        _add_scalar_constant_plan(plans, bv, slot_addr, width, use_addr)
    return [plans[addr] for addr in sorted(plans)]


def plan_string_decrypt_calls(bv, func, il, mlil_stable):
    return []
