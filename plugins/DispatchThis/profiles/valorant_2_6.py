from binaryninja import LowLevelILOperation as L, MediumLevelILOperation as M

from . import default
from ..helpers import facts, llil, memory, mlil
from ..utils.log import log_debug, log_warn


PROFILE_ID = "valorant_2_6"
PROFILE_NAME = "Valorant 2.6"
PROFILE_DESCRIPTION = (
    "Rules for the valorant_2_6 binary: main branch and call gadgets; "
    "global constants; loop and unrolled string decrypt clones."
)

# Supported:
# - branch gadget: yes
# - indirect call gadget: yes
# - global constants: yes
# - deflatten: default planner
# - string decrypt: yes (rem-loop, index0-loop, unrolled; ignores mlil_stable)
#
# Validation:
# - branch: 0x6c5f6c -> 0x6c5f70, 0x6c6a4c
# - call: 0x6c5ee0 -> 0x6cb194
# - string decrypt (live main const 2-arg sweep: 11 hits):
#   - rem-loop 0x6c7a28 -> 0x6da548 / 0x129e304
#   - unrolled 0x6c7b34 -> 0x6da5c8 / 0x129e39c
#   - index0-loop 0x6c9e08 -> 0x6da834 / 0x129e522

U64 = 0xFFFFFFFFFFFFFFFF
MAIN_BRANCH_KEY = 0x5C76880DE50178C9
MAIN_START = 0x6C5E04
CONST_SLOT_TYPE = "void const* const"
GLOBAL_QWORD_CONST_TYPE = "uint64_t const"
_CONST_DATA_SECTIONS = {".data", ".rodata"}
_MUTABLE_SCALAR_SECTIONS = {".bss"}
_GLOBAL_CONSTANT_SLOT_RANGE = range(0x12A01E0, 0x12A0E38, 8)
# Audited true-pointer slots. 0x11f5660 is mutable and intentionally excluded.
_GLOBAL_POINTER_CONST_SLOTS = {0x11F5658: "char const* const"}
_SCALAR_CONSTANT_BLOB_RANGE = (0x11F5678, 0x11F5878)
_SCALAR_CONST_TYPES = {
    1: "uint8_t const",
    2: "uint16_t const",
    4: "uint32_t const",
    8: "uint64_t const",
}
_SCALAR_CONST_WIDTHS = {type_name: width for width, type_name in _SCALAR_CONST_TYPES.items()}

_CONST_OPS = (*llil.CONST_OPS, *mlil.CONST_OPS)
_LOAD_OPS = (*llil.LOAD_OPS, *mlil.LOAD_OPS)
_SET_OPS = (*llil.SET_REG_OPS, *mlil.SET_VAR_OPS, M.MLIL_SET_VAR_SSA.name)
_PHI_OPS = (L.LLIL_REG_PHI.name, M.MLIL_VAR_PHI.name)
_CALL_OPS = tuple(op.name for op in (M.MLIL_CALL, M.MLIL_CALL_SSA, M.MLIL_CALL_UNTYPED))
_CMP_NE_OPS = (M.MLIL_CMP_NE.name,)
_STORE_OPS = mlil.STORE_OPS
_VAR_OPS = tuple(op.name for op in (M.MLIL_VAR, M.MLIL_VAR_SSA, M.MLIL_VAR_ALIASED, M.MLIL_VAR_FIELD))
_PURE_JOIN_OPS = tuple(op.name for op in (M.MLIL_SET_VAR, M.MLIL_SET_VAR_FIELD, M.MLIL_NOP))
_CAST_OPS = tuple(
    op.name
    for op in (L.LLIL_ZX, L.LLIL_SX, L.LLIL_LOW_PART, M.MLIL_ZX, M.MLIL_SX, M.MLIL_LOW_PART)
)
_SX_OPS = (L.LLIL_SX.name, M.MLIL_SX.name)


def _expression_key(expr):
    expr_index = getattr(expr, "expr_index", None)
    instr_index = getattr(expr, "instr_index", None)
    if expr_index is None and instr_index is None:
        return ("object", id(expr))
    return ("il", id(getattr(expr, "function", None)), expr_index, instr_index)


def plan_deflatten_redirections(bv, func, il):
    return default.plan_deflatten_redirections(bv, func, il)


def _iter_scalar_constant_loads(bv, il):
    start, end = _SCALAR_CONSTANT_BLOB_RANGE
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _LOAD_OPS:
                continue
            width = getattr(expr, "size", 0)
            if width not in _SCALAR_CONST_TYPES:
                continue
            offset = getattr(expr, "offset", 0) or 0
            addresses = _values(bv, il, expr.src)
            if addresses is None:
                continue
            for addr in addresses:
                addr += offset
                if start <= addr and addr + width <= end:
                    yield addr, width


def _add_global_constant_plan(plans, bv, slot_addr, type_name):
    if slot_addr in plans:
        return
    if not memory.in_section(bv, slot_addr, _CONST_DATA_SECTIONS):
        return
    value = memory.read_qword_slot(bv, slot_addr)
    if value is None:
        return
    plans[slot_addr] = facts.global_constant_fact(slot_addr, type_name)


def _add_scalar_constant_plan(plans, bv, slot_addr, width):
    type_name = _SCALAR_CONST_TYPES.get(width)
    if type_name is None or not memory.in_section(bv, slot_addr, _CONST_DATA_SECTIONS):
        return
    existing = plans.get(slot_addr)
    if existing is not None and width <= _SCALAR_CONST_WIDTHS.get(existing.get("type"), 0):
        return
    value = memory.read_uint_le(bv, slot_addr, width)
    if value is None:
        return
    plans[slot_addr] = facts.global_constant_fact(slot_addr, type_name)


def _op(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def _expr_mask(expr):
    size = getattr(expr, "size", None)
    return (1 << size * 8) - 1 if type(size) is int and size > 0 else U64


def _cast_value(op, expr, value):
    if op in _SX_OPS:
        source = getattr(expr, "src", None)
        source_mask = _expr_mask(source)
        value &= source_mask
        sign = (source_mask + 1) >> 1
        if value & sign:
            value -= source_mask + 1
    return value & _expr_mask(expr)


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


def _complete_union(value_sets):
    """Union complete value sets; one unknown input makes the result unknown."""
    out = set()
    for values in value_sets:
        if values is None:
            return None
        out.update(values)
    return out


def _values_for_phi_operand(
    bv,
    il,
    operand,
    depth,
    max_depth,
    seen,
    bindings=None,
    memory_read_allowed=None,
):
    if hasattr(operand, "operation"):
        return _values(bv, il, operand, depth, max_depth, seen, bindings, memory_read_allowed)
    definition = _definition(il, operand) or _ssa_var_definition(il, operand)
    if definition is not None:
        return _values(bv, il, definition, depth, max_depth, seen, bindings, memory_read_allowed)
    definitions = _var_definitions(il, operand)
    if not definitions:
        return None
    return _complete_union(
        _values(
            bv,
            il,
            definition,
            depth,
            max_depth,
            seen.copy(),
            bindings,
            memory_read_allowed,
        )
        for definition in definitions
    )


def _bound_value(bindings, var):
    if not bindings:
        return None
    return bindings.get(var)


def _values(bv, il, expr, depth=0, max_depth=64, seen=None, bindings=None, memory_read_allowed=None):
    if expr is None or depth > max_depth:
        return None
    if seen is None:
        seen = set()
    key = _expression_key(expr)
    if key in seen:
        return None
    seen.add(key)

    op = _op(expr)
    if op in _CONST_OPS:
        return {expr.constant & _expr_mask(expr)}

    if op in _CAST_OPS:
        values = _values(
            bv, il, expr.src, depth + 1, max_depth, seen, bindings, memory_read_allowed
        )
        return None if values is None else {_cast_value(op, expr, value) for value in values}

    if op in (L.LLIL_REG_SSA.name, L.LLIL_REG.name):
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & _expr_mask(expr)}
        definition = _definition(il, expr.src)
        values = None if definition is None else _values(
            bv, il, definition, depth + 1, max_depth, seen, bindings, memory_read_allowed
        )
        return None if values is None else {value & _expr_mask(expr) for value in values}

    if op == M.MLIL_VAR_SSA.name:
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & _expr_mask(expr)}
        definition = _ssa_var_definition(il, expr.src)
        values = None if definition is None else _values(
            bv, il, definition, depth + 1, max_depth, seen, bindings, memory_read_allowed
        )
        return None if values is None else {value & _expr_mask(expr) for value in values}

    if op == M.MLIL_VAR.name:
        definitions = _var_definitions(il, expr.src)
        if not definitions:
            return None
        values = _complete_union(
            _values(
                bv,
                il,
                definition,
                depth + 1,
                max_depth,
                seen.copy(),
                bindings,
                memory_read_allowed,
            )
            for definition in definitions
        )
        return None if values is None else {value & _expr_mask(expr) for value in values}

    if op in _SET_OPS:
        return _values(bv, il, expr.src, depth + 1, max_depth, seen, bindings, memory_read_allowed)

    if op in _PHI_OPS:
        operands = tuple(getattr(expr, "src", ()) or ())
        if not operands:
            return None
        return _complete_union(
            _values_for_phi_operand(
                bv,
                il,
                operand,
                depth + 1,
                max_depth,
                seen.copy(),
                bindings,
                memory_read_allowed,
            )
            for operand in operands
        )

    if op in _LOAD_OPS:
        stack_sources = llil.stack_store_sources(il, expr)
        if stack_sources:
            return _complete_union(
                _values(
                    bv,
                    il,
                    source,
                    depth + 1,
                    max_depth,
                    seen.copy(),
                    bindings,
                    memory_read_allowed,
                )
                for source in stack_sources
            )
        addresses = _values(
            bv, il, expr.src, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
        )
        if addresses is None:
            return None
        out = set()
        size = getattr(expr, "size", 8)
        offset = 0
        if op in mlil.LOAD_STRUCT_OPS:
            offset = getattr(expr, "offset", None)
            if type(offset) is not int:
                return None
        for addr in addresses:
            addr += offset
            if memory_read_allowed is not None and not memory_read_allowed(addr, size):
                return None
            value = memory.read_uint_le(bv, addr, size)
            if value is None:
                return None
            out.add(value & _expr_mask(expr))
        return out

    if op in (L.LLIL_NEG.name, M.MLIL_NEG.name):
        values = _values(
            bv, il, expr.src, depth + 1, max_depth, seen, bindings, memory_read_allowed
        )
        return None if values is None else {(-value) & _expr_mask(expr) for value in values}

    if op in (
        L.LLIL_ADD.name,
        M.MLIL_ADD.name,
        L.LLIL_SUB.name,
        M.MLIL_SUB.name,
        L.LLIL_MUL.name,
        M.MLIL_MUL.name,
        L.LLIL_AND.name,
        M.MLIL_AND.name,
        L.LLIL_OR.name,
        M.MLIL_OR.name,
        L.LLIL_XOR.name,
        M.MLIL_XOR.name,
        L.LLIL_LSL.name,
        M.MLIL_LSL.name,
        L.LLIL_LSR.name,
        M.MLIL_LSR.name,
    ):
        lefts = _values(
            bv, il, expr.left, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
        )
        rights = _values(
            bv, il, expr.right, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
        )
        if lefts is None or rights is None:
            return None
        mask = _expr_mask(expr)
        out = set()
        for left in lefts:
            for right in rights:
                if op.endswith("_ADD"):
                    out.add((left + right) & mask)
                elif op.endswith("_SUB"):
                    out.add((left - right) & mask)
                elif op.endswith("_MUL"):
                    out.add((left * right) & mask)
                elif op.endswith("_AND"):
                    out.add((left & right) & mask)
                elif op.endswith("_OR"):
                    out.add((left | right) & mask)
                elif op.endswith("_XOR"):
                    out.add((left ^ right) & mask)
                elif op.endswith("_LSL"):
                    out.add((left << right) & mask)
                elif op.endswith("_LSR"):
                    out.add((left >> right) & mask)
        return out

    return None


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
    phi_regs = tuple(llil.phi_registers(ssa, dest, max_depth=64))
    if len(phi_regs) != 1:
        return _values(bv, ssa, dest)

    out = set()
    phi_reg = phi_regs[0]
    phi_values = _values(bv, ssa, _definition(ssa, phi_reg))
    if phi_values is None:
        return None
    for value in phi_values:
        bindings = {phi_reg: value}
        values = _values(bv, ssa, dest, bindings=bindings)
        if values is None:
            return None
        out.update(values)
    return out


def _validated_targets(values, is_valid):
    """Validate exact IL-derived targets without inventing address aliases."""
    if not values:
        return None
    targets = tuple(sorted(values))
    return (
        targets
        if all(type(target) is int and target >= 0 and is_valid(target) for target in targets)
        else None
    )


def _jump_dest(jump_il):
    return getattr(getattr(jump_il, "ssa_form", None), "dest", None) or getattr(jump_il, "dest", None)


def _valid_branch_target(bv, target):
    return (
        target % 4 == 0
        and memory.is_executable_target(bv, target)
        and memory.in_section(bv, target, ".text")
    )


def resolve_branch_gadget(bv, il, known_targets=None):
    if not il:
        return []
    ssa = getattr(il, "ssa_form", il)
    out = []
    for jump_il in llil.iter_indirect_jumps(il):
        values = _branch_values(bv, ssa, _jump_dest(jump_il))
        targets = None if values is None else _validated_targets(
            values,
            lambda target: _valid_branch_target(bv, target),
        )
        if targets:
            out.append(facts.branch_fact(
                jump_il.address,
                jump_il.dest.expr_index,
                targets,
                jump_il=jump_il,
            ))
    return out


def _single_decode_def(il, dest):
    if _op(dest) not in (M.MLIL_VAR.name, M.MLIL_VAR_SSA.name):
        return None
    defs = [definition for definition in _var_definitions(il, dest.src) if _op(definition) in _SET_OPS]
    return defs[0] if len(defs) == 1 else None


def _valid_call_target(bv, target):
    return memory.is_known_callee(bv, target)


def _call_dest_values(bv, il, call_il):
    ssa = getattr(il, "ssa_form", None)
    ssa_dest = getattr(getattr(call_il, "ssa_form", None), "dest", None)
    if ssa is not None and ssa_dest is not None:
        return _values(bv, ssa, ssa_dest)
    return _values(bv, il, call_il.dest)


def resolve_call_gadget(bv, il):
    if il is None:
        return []

    out = []
    for call_il in mlil.iter_indirect_calls(il):
        targets = _validated_targets(
            _call_dest_values(bv, il, call_il),
            lambda target: _valid_call_target(bv, target),
        )
        if targets is None or len(targets) != 1:
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
        _add_global_constant_plan(plans, bv, slot_addr, GLOBAL_QWORD_CONST_TYPE)
    for slot_addr, type_name in _GLOBAL_POINTER_CONST_SLOTS.items():
        _add_global_constant_plan(plans, bv, slot_addr, type_name)
    for slot_addr, width in _iter_scalar_constant_loads(bv, il):
        _add_scalar_constant_plan(plans, bv, slot_addr, width)
    return [plans[addr] for addr in sorted(plans)]


def plan_correlated_store_rewrites(bv, func, il):
    """Plan arm-local stores when a main-function join loses PHI correlation."""
    if il is None or getattr(func, "start", None) != MAIN_START:
        return []
    ssa = getattr(il, "ssa_form", None)
    if ssa is None:
        return []

    plans = []
    for store in getattr(ssa, "instructions", ()) or ():
        plan = _plan_correlated_store(bv, il, ssa, store)
        if plan is not None:
            plans.append(plan)
    return sorted(plans, key=lambda plan: getattr(plan["store"], "instr_index", 0))


def _plan_correlated_store(bv, il, ssa, store):
    if _op(store) != M.MLIL_STORE_SSA.name or getattr(store, "size", None) != 4:
        return None
    join = getattr(store, "il_basic_block", None)
    edges = list(getattr(join, "incoming_edges", ()) or ())
    if len(edges) != 2:
        return None
    predecessors = [getattr(edge, "source", None) for edge in edges]
    if any(predecessor is None for predecessor in predecessors):
        return None
    if any(len(getattr(predecessor, "outgoing_edges", ()) or ()) != 1 for predecessor in predecessors):
        return None

    phi_defs = _store_phi_defs(ssa, store)
    if len(phi_defs) < 2:
        return None
    predecessor_starts = tuple(getattr(predecessor, "start", None) for predecessor in predecessors)
    if any(
        len(getattr(definition, "src", ()) or ()) != 2
        or tuple(_definition_block_start(ssa, value) for value in definition.src) != predecessor_starts
        for definition in phi_defs.values()
    ):
        return None

    source_load = _peel_ssa_value(ssa, getattr(store, "src", None))
    if _op(source_load) != M.MLIL_LOAD_SSA.name or getattr(source_load, "size", None) != store.size:
        return None

    non_ssa_store = getattr(store, "non_ssa_form", None)
    ssa_gotos = [_block_terminal(ssa, predecessor) for predecessor in predecessors]
    if any(_op(goto) != M.MLIL_GOTO.name for goto in ssa_gotos):
        return None
    non_ssa_gotos = [getattr(goto, "non_ssa_form", None) for goto in ssa_gotos]
    if _op(non_ssa_store) != M.MLIL_STORE.name or any(
        _op(goto) != M.MLIL_GOTO.name for goto in non_ssa_gotos
    ):
        return None
    if not _pure_join_prefix(il, non_ssa_store):
        return None

    def memory_read_allowed(address, size):
        return _read_only_global_load(bv, address, size)
    arms = []
    for arm_index, goto in enumerate(non_ssa_gotos):
        bindings = _phi_bindings(bv, ssa, phi_defs, arm_index, memory_read_allowed)
        if bindings is None:
            return None
        destinations = _values(
            bv,
            ssa,
            getattr(store, "dest", None),
            bindings=bindings,
            memory_read_allowed=memory_read_allowed,
        )
        sources = _values(
            bv,
            ssa,
            getattr(source_load, "src", None),
            bindings=bindings,
            memory_read_allowed=memory_read_allowed,
        )
        if destinations is None or sources is None or len(destinations) != 1 or len(sources) != 1:
            return None
        destination = next(iter(destinations))
        source = next(iter(sources))
        if destination == source or not _mutable_scalar(bv, destination, store.size):
            return None
        if not _mutable_scalar(bv, source, store.size):
            return None
        arms.append({"goto": goto, "dest": destination, "src": source})

    if (
        len({getattr(arm["goto"], "instr_index", id(arm["goto"])) for arm in arms}) != 2
        or arms[0]["dest"] != arms[1]["src"]
        or arms[0]["src"] != arms[1]["dest"]
    ):
        return None
    return {"store": non_ssa_store, "size": store.size, "arms": tuple(arms)}


def _store_phi_defs(ssa, store):
    out = {}
    seen = set()
    queue = list(getattr(store, "vars_read", ()) or ())
    while queue:
        variable = queue.pop()
        if variable in seen:
            continue
        seen.add(variable)
        definition = _ssa_var_definition(ssa, variable)
        if _op(definition) == M.MLIL_VAR_PHI.name:
            out[variable] = definition
        elif definition is not None:
            queue.extend(getattr(definition, "vars_read", ()) or ())
    return out


def _definition_block_start(ssa, var):
    definition = _ssa_var_definition(ssa, var)
    return getattr(getattr(definition, "il_basic_block", None), "start", None)


def _peel_ssa_value(ssa, expr):
    for _ in range(64):
        if _op(expr) == M.MLIL_VAR_SSA.name:
            expr = getattr(_ssa_var_definition(ssa, expr.src), "src", None)
            continue
        if _op(expr) in (
            M.MLIL_SET_VAR_SSA.name,
            M.MLIL_SET_VAR.name,
            M.MLIL_SET_VAR_SSA_FIELD.name,
            M.MLIL_SET_VAR_FIELD.name,
        ):
            expr = getattr(expr, "src", None)
            continue
        return expr
    return None


def _block_terminal(il, block):
    try:
        return il[block.end - 1]
    except Exception:  # noqa: BLE001
        return None


def _pure_join_prefix(il, store):
    block = getattr(store, "il_basic_block", None)
    if block is None:
        return False
    for index in range(block.start, block.end):
        instruction = il[index]
        if getattr(instruction, "instr_index", None) == getattr(store, "instr_index", None):
            return True
        if (
            _op(instruction) not in _PURE_JOIN_OPS
            or mlil.has_unmodeled_semantics(instruction)
            or mlil.expression_has_operation(instruction, mlil.LOAD_OPERATIONS)
        ):
            return False
    return False


def _phi_bindings(bv, ssa, phi_defs, arm_index, memory_read_allowed=None):
    bindings = {}
    for var, definition in phi_defs.items():
        values = _values_for_phi_operand(
            bv,
            ssa,
            definition.src[arm_index],
            0,
            64,
            set(),
            None,
            memory_read_allowed,
        )
        if values is None or len(values) != 1:
            return None
        value = next(iter(values))
        bindings[var] = value
    return bindings


def _read_only_global_load(bv, address, width):
    if not memory.in_section(bv, address, _CONST_DATA_SECTIONS):
        return False
    data_var = bv.get_data_var_at(address)
    type_ = getattr(data_var, "type", None)
    return getattr(type_, "width", None) == width and bool(
        getattr(type_, "const", False)
    )


def _mutable_scalar(bv, address, width):
    if not memory.in_section(bv, address, _MUTABLE_SCALAR_SECTIONS):
        return False
    data_var = bv.get_data_var_at(address)
    type_ = getattr(data_var, "type", None)
    return getattr(type_, "width", None) == width and not bool(
        getattr(type_, "const", False)
    )


def _mlil_const(il, expr):
    return mlil.expression_scalar_value(il, expr)


def _parameters(func, il):
    for owner in (func, getattr(il, "source_function", None)):
        params = getattr(owner, "parameter_vars", None)
        if params:
            return list(params)
    return []


def _has_done_flag_store(il):
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _STORE_OPS or getattr(expr, "size", None) != 1:
                continue
            if _mlil_const(il, getattr(expr, "src", None)) == 1:
                return True
    return False


def _has_byte_crypto_store(il):
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _STORE_OPS or getattr(expr, "size", None) != 1:
                continue
            if _mlil_const(il, getattr(expr, "src", None)) == 1:
                continue
            # XOR/NOT often sits on the defining assignment, not the store leaf.
            ops = {
                _op(child)
                for child in mlil.walk_expr_with_defs(il, getattr(expr, "src", None))
            }
            if ops & {M.MLIL_XOR.name, M.MLIL_NOT.name, M.MLIL_NEG.name}:
                return True
    return False


def _rem_moduli(il):
    """Strength-reduced `i % M` appears as `i - q * M`."""
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) != M.MLIL_SUB.name:
                continue
            right = getattr(expr, "right", None)
            if _op(right) != M.MLIL_MUL.name:
                continue
            for side in (getattr(right, "left", None), getattr(right, "right", None)):
                if _op(side) not in _CONST_OPS:
                    continue
                value = side.constant
                if isinstance(value, int) and 2 <= value <= 256:
                    out.add(value)
    return out


def _cmp_ne_constants(il):
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _CMP_NE_OPS:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                if _op(side) not in _CONST_OPS:
                    continue
                value = side.constant
                if isinstance(value, int) and value > 1:
                    out.add(value)
    return out


def _relative_const_offset(expr):
    """Return const displacement for `var` / `var+const` style addresses."""
    if expr is None:
        return None
    if _op(expr) in _VAR_OPS:
        return 0
    if _op(expr) == M.MLIL_ADD.name:
        left, right = getattr(expr, "left", None), getattr(expr, "right", None)
        if _op(left) in _CONST_OPS and _op(right) not in _CONST_OPS:
            value = left.constant
            return value if isinstance(value, int) else None
        if _op(right) in _CONST_OPS and _op(left) not in _CONST_OPS:
            value = right.constant
            return value if isinstance(value, int) else None
    if _op(expr) == M.MLIL_SUB.name:
        left, right = getattr(expr, "left", None), getattr(expr, "right", None)
        if _op(right) in _CONST_OPS and _op(left) not in _CONST_OPS:
            value = right.constant
            if isinstance(value, int):
                return -value
    return None


def _and_moduli(il):
    """`i % (mask+1)` sometimes appears as `i & mask` with mask = 2^n - 1."""
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) != M.MLIL_AND.name:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                if _op(side) not in _CONST_OPS:
                    continue
                value = side.constant
                if isinstance(value, int) and value in (0x3, 0x7, 0xF, 0x1F, 0x3F, 0x7F, 0xFF):
                    out.add(value + 1)
    return out


def _payload_const_offsets(il):
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) in _LOAD_OPS and getattr(expr, "size", None) == 1:
                offset = _relative_const_offset(getattr(expr, "src", None))
                if offset is not None and offset > 0:
                    out.add(offset)
        # payload base may be hoisted: x = arg2 + M; load [x + i]
        if _op(ins) not in (
            M.MLIL_SET_VAR.name,
            M.MLIL_SET_VAR_FIELD.name,
            M.MLIL_SET_VAR_SSA.name,
        ):
            continue
        src = getattr(ins, "src", None)
        if _op(src) != M.MLIL_ADD.name:
            continue
        for side in (getattr(src, "left", None), getattr(src, "right", None)):
            if _op(side) not in _CONST_OPS:
                continue
            value = side.constant
            if isinstance(value, int) and 2 <= value <= 256:
                out.add(value)
    return out


def _recognize_rem_loop_string_decrypt(il):
    """Loop with strength-reduced rem: index starts at M, end_exclusive = M + length."""
    if not _has_done_flag_store(il) or not _has_byte_crypto_store(il):
        return None
    rem = _rem_moduli(il)
    if not rem:
        return None
    ends = _cmp_ne_constants(il)
    specs = {
        (modulus, bound - modulus)
        for modulus in rem
        for bound in ends
        if 0 < bound - modulus <= 4096
    }
    return _unique_string_spec(specs)


def _recognize_index0_loop_string_decrypt(il):
    """Loop with i from 0: `if (i != length)` and payload at src[M + i]."""
    if not _has_done_flag_store(il) or not _has_byte_crypto_store(il):
        return None
    if _rem_moduli(il):
        return None
    ends = {bound for bound in _cmp_ne_constants(il) if 1 < bound <= 4096}
    if len(ends) != 1:
        return None
    length = next(iter(ends))
    and_moduli = _and_moduli(il)
    if and_moduli:
        return _unique_string_spec({(modulus, length) for modulus in and_moduli})
    # Bare key index `i` requires length <= M; M is the fixed payload base offset.
    return _unique_string_spec({
        (modulus, length)
        for modulus in _payload_const_offsets(il)
        if 2 <= modulus <= 256 and length <= modulus
    })


def _recognize_unrolled_string_decrypt(il):
    if not _has_done_flag_store(il) or not _has_byte_crypto_store(il):
        return None
    if _rem_moduli(il) or _and_moduli(il):
        return None
    load_offsets = set()
    store_offsets = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) in _LOAD_OPS and getattr(expr, "size", None) == 1:
                offset = _relative_const_offset(getattr(expr, "src", None))
                if offset is not None and offset > 0:
                    load_offsets.add(offset)
            if _op(expr) not in _STORE_OPS or getattr(expr, "size", None) != 1:
                continue
            if _mlil_const(il, getattr(expr, "src", None)) == 1:
                continue
            offset = _relative_const_offset(getattr(expr, "dest", None))
            if offset is not None and offset >= 0:
                store_offsets.add(offset)

    if not store_offsets or not load_offsets:
        return None
    length = max(store_offsets) + 1
    if length > 4096 or set(range(length)) != store_offsets:
        return None

    # Payload bytes sit at src[M + i]; recover M from a contiguous load run of size length.
    # Prefer the largest matching base so sparse key-index loads below M are not chosen.
    matches = []
    for modulus in sorted(offset for offset in load_offsets if offset >= length):
        expected = {modulus + index for index in range(length)}
        if expected <= load_offsets:
            matches.append(modulus)
    return _unique_string_spec({(modulus, length) for modulus in matches})


def _unique_string_spec(specs):
    if len(specs) != 1:
        return None
    modulus, length = next(iter(specs))
    return {"key_modulus": modulus, "length": length}


def _recognize_string_decrypt_function(func, il=None):
    il = il or getattr(func, "mlil", None) or getattr(func, "medium_level_il", None)
    if il is None or len(_parameters(func, il)) < 2:
        return None
    specs = {
        (spec["key_modulus"], spec["length"])
        for spec in (
            _recognize_rem_loop_string_decrypt(il),
            _recognize_index0_loop_string_decrypt(il),
            _recognize_unrolled_string_decrypt(il),
        )
        if spec is not None
    }
    return _unique_string_spec(specs)


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
    previous = 0
    for index, encoded in enumerate(payload):
        key_index = index % key_modulus
        key_byte = key[key_index]
        if ((key_index * key_byte) & 1) == 0:
            decoded = ((previous + encoded) & 0xFF) ^ ((~key_byte) & 0xFF)
        else:
            decoded = (-(((encoded - previous) & 0xFF) ^ key_byte)) & 0xFF
        plain = decoded ^ key_byte
        out.append(plain)
        previous = plain
    return bytes(out)


def plan_string_decrypt_calls(bv, _func, il, _mlil_stable):
    """Plan decrypt comments for const 2-arg calls.

    Valorant decrypt clones are plain functions, so callee deflatten / mlil_stable
    receipts are intentionally ignored.
    """
    if il is None:
        return []

    out = []
    for call in mlil.iter_calls(il, _CALL_OPS):
        target = _mlil_const(il, getattr(call, "dest", None))
        params = list(getattr(call, "params", ()) or ())
        if target is None or len(params) < 2:
            continue
        dst_addr = _mlil_const(il, params[0])
        src_addr = _mlil_const(il, params[1])
        if dst_addr is None or src_addr is None:
            continue
        callee = bv.get_function_at(target)
        if callee is None:
            log_debug(f"[valorant_2_6:sdecrypt] {hex(call.address)}: no function at {hex(target)}")
            continue
        spec = _recognize_string_decrypt_function(callee)
        if spec is None:
            continue
        plaintext = _decode_string_blob(bv, src_addr, spec)
        if plaintext is None:
            log_warn(
                f"[valorant_2_6:sdecrypt] {hex(call.address)}: "
                f"source blob @ {hex(src_addr)} is too short for {spec}"
            )
            continue
        out.append(facts.string_decrypt_fact(call.address, src_addr, dst_addr, plaintext))
    return out
