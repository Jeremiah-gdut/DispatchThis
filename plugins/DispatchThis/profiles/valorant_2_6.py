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

U48 = llil.U48
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
_SET_OPS = (*llil.SET_REG_OPS, *mlil.SET_VAR_OPS, "MLIL_SET_VAR_SSA")
_PHI_OPS = ("LLIL_REG_PHI", "MLIL_VAR_PHI")
_CALL_OPS = ("MLIL_CALL", "MLIL_CALL_SSA", "MLIL_CALL_UNTYPED")
_CMP_NE_OPS = ("MLIL_CMP_NE",)
_STORE_OPS = mlil.STORE_OPS
_VAR_OPS = ("MLIL_VAR", "MLIL_VAR_SSA", "MLIL_VAR_ALIASED", "MLIL_VAR_FIELD")
_PURE_JOIN_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD", "MLIL_NOP")


def plan_deflatten_redirections(bv, func, il):
    return default.plan_deflatten_redirections(bv, func, il)


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
    out = set()
    for definition in _var_definitions(il, operand):
        out.update(_values(bv, il, definition, depth, max_depth, seen.copy(), bindings, memory_read_allowed))
    return out


def _bound_value(bindings, var):
    if not bindings:
        return None
    if var in bindings:
        return bindings[var]
    return bindings.get(str(var))


def _values(bv, il, expr, depth=0, max_depth=64, seen=None, bindings=None, memory_read_allowed=None):
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
        return _values(bv, il, expr.src, depth + 1, max_depth, seen, bindings, memory_read_allowed)

    if op in ("LLIL_REG_SSA", "LLIL_REG"):
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & U64}
        definition = _definition(il, expr.src)
        return set() if definition is None else _values(
            bv, il, definition, depth + 1, max_depth, seen, bindings, memory_read_allowed
        )

    if op == "MLIL_VAR_SSA":
        bound = _bound_value(bindings, expr.src)
        if bound is not None:
            return {bound & U64}
        definition = _ssa_var_definition(il, expr.src)
        return set() if definition is None else _values(
            bv, il, definition, depth + 1, max_depth, seen, bindings, memory_read_allowed
        )

    if op == "MLIL_VAR":
        out = set()
        for definition in _var_definitions(il, expr.src):
            out.update(_values(
                bv, il, definition, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
            ))
        return out

    if op in _SET_OPS:
        return _values(bv, il, expr.src, depth + 1, max_depth, seen, bindings, memory_read_allowed)

    if op in _PHI_OPS:
        out = set()
        for operand in getattr(expr, "src", ()) or ():
            out.update(_values_for_phi_operand(
                bv, il, operand, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
            ))
        return out

    if op in _LOAD_OPS:
        stack_src = _stack_store_source(il, expr)
        if stack_src is not None:
            return _values(
                bv, il, stack_src, depth + 1, max_depth, seen, bindings, memory_read_allowed
            )
        out = set()
        size = getattr(expr, "size", 8)
        for addr in _values(
            bv, il, expr.src, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
        ):
            addr &= U48
            if memory_read_allowed is not None and not memory_read_allowed(addr, size):
                continue
            value = memory.read_uint_le(bv, addr, size)
            if value is not None:
                out.add(value & U64)
        return out

    if op in ("LLIL_NEG", "MLIL_NEG"):
        return {
            (-value) & U64
            for value in _values(
                bv, il, expr.src, depth + 1, max_depth, seen, bindings, memory_read_allowed
            )
        }

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
        lefts = _values(
            bv, il, expr.left, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
        )
        rights = _values(
            bv, il, expr.right, depth + 1, max_depth, seen.copy(), bindings, memory_read_allowed
        )
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
        symbol = bv.get_symbol_at(target)
        if symbol is not None:
            return getattr(getattr(symbol, "type", None), "name", None) in (
                "ExternalSymbol",
                "FunctionSymbol",
                "ImportedFunctionSymbol",
                "LibraryFunctionSymbol",
                "SymbolicFunctionSymbol",
            )
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
        _add_global_constant_plan(plans, bv, slot_addr, GLOBAL_QWORD_CONST_TYPE)
    for slot_addr, type_name in _GLOBAL_POINTER_CONST_SLOTS.items():
        _add_global_constant_plan(plans, bv, slot_addr, type_name)
    for slot_addr, width, use_addr in _iter_scalar_constant_loads(bv, il):
        _add_scalar_constant_plan(plans, bv, slot_addr, width, use_addr)
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
    if _op(store) != "MLIL_STORE_SSA" or getattr(store, "size", None) != 4:
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
    if _op(source_load) != "MLIL_LOAD_SSA" or getattr(source_load, "size", None) != store.size:
        return None

    non_ssa_store = getattr(store, "non_ssa_form", None)
    ssa_gotos = [_block_terminal(ssa, predecessor) for predecessor in predecessors]
    if any(_op(goto) != "MLIL_GOTO" for goto in ssa_gotos):
        return None
    non_ssa_gotos = [getattr(goto, "non_ssa_form", None) for goto in ssa_gotos]
    if _op(non_ssa_store) != "MLIL_STORE" or any(_op(goto) != "MLIL_GOTO" for goto in non_ssa_gotos):
        return None
    if not _pure_join_prefix(il, non_ssa_store):
        return None

    memory_read_allowed = lambda address, size: _read_only_global_load(bv, address, size)
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
        if len(destinations) != 1 or len(sources) != 1:
            return None
        destination = next(iter(destinations)) & U48
        source = next(iter(sources)) & U48
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
    seen_exprs = set()
    seen_vars = set()

    def visit(expr, depth=0):
        if expr is None or depth > 64:
            return
        op = _op(expr)
        if op == "MLIL_VAR_SSA":
            var = expr.src
            key = str(var)
            if key in seen_vars:
                return
            seen_vars.add(key)
            definition = _ssa_var_definition(ssa, var)
            if _op(definition) == "MLIL_VAR_PHI":
                out[var] = definition
                return
            visit(getattr(definition, "src", None), depth + 1)
            return
        key = getattr(expr, "expr_index", id(expr))
        if key in seen_exprs:
            return
        seen_exprs.add(key)
        for name in ("src", "dest", "left", "right", "condition"):
            child = getattr(expr, name, None)
            if hasattr(child, "operation"):
                visit(child, depth + 1)
        for child in getattr(expr, "params", ()) or ():
            if hasattr(child, "operation"):
                visit(child, depth + 1)

    visit(getattr(store, "dest", None))
    visit(getattr(store, "src", None))
    return out


def _definition_block_start(ssa, var):
    definition = _ssa_var_definition(ssa, var)
    return getattr(getattr(definition, "il_basic_block", None), "start", None)


def _peel_ssa_value(ssa, expr):
    for _ in range(64):
        if _op(expr) == "MLIL_VAR_SSA":
            expr = getattr(_ssa_var_definition(ssa, expr.src), "src", None)
            continue
        if _op(expr) in ("MLIL_SET_VAR_SSA", "MLIL_SET_VAR", "MLIL_SET_VAR_FIELD_SSA", "MLIL_SET_VAR_FIELD"):
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
        if _op(instruction) not in _PURE_JOIN_OPS:
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
        if len(values) != 1:
            return None
        value = next(iter(values))
        bindings[var] = value
        bindings[str(var)] = value
    return bindings


def _read_only_global_load(bv, address, width):
    if not memory.in_section(bv, address, _CONST_DATA_SECTIONS):
        return False
    data_var = bv.get_data_var_at(address)
    type_ = getattr(data_var, "type", None)
    return getattr(type_, "width", None) == width and "const" in str(type_)


def _mutable_scalar(bv, address, width):
    if not memory.in_section(bv, address, _MUTABLE_SCALAR_SECTIONS):
        return False
    data_var = bv.get_data_var_at(address)
    type_ = getattr(data_var, "type", None)
    return getattr(type_, "width", None) == width and "const" not in str(type_)


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
            if "MLIL_XOR" in ops or "MLIL_NOT" in ops or "MLIL_NEG" in ops:
                return True
    return False


def _rem_moduli(il):
    """Strength-reduced `i % M` appears as `i - q * M`."""
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) != "MLIL_SUB":
                continue
            right = getattr(expr, "right", None)
            if _op(right) != "MLIL_MUL":
                continue
            for side in (getattr(right, "left", None), getattr(right, "right", None)):
                if _op(side) not in _CONST_OPS:
                    continue
                value = side.constant
                if isinstance(value, int) and 2 <= value <= 256:
                    out.add(value)
    return out


def _init_moduli(il):
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        if _op(ins) not in ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD", "MLIL_SET_VAR_SSA"):
            continue
        src = getattr(ins, "src", None)
        if _op(src) not in _CONST_OPS:
            continue
        value = src.constant
        if isinstance(value, int) and 2 <= value <= 256:
            out.add(value)
    return out


def _store_modulus_offsets(il):
    out = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if _op(expr) not in _STORE_OPS or getattr(expr, "size", None) != 1:
                continue
            dest = getattr(expr, "dest", None)
            if _op(dest) == "MLIL_ADD":
                for side in (getattr(dest, "left", None), getattr(dest, "right", None)):
                    if _op(side) not in _CONST_OPS:
                        continue
                    value = side.constant
                    if isinstance(value, int) and -256 <= value <= -2:
                        out.add(-value)
            elif _op(dest) == "MLIL_SUB":
                right = getattr(dest, "right", None)
                if _op(right) in _CONST_OPS:
                    value = right.constant
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
    if _op(expr) == "MLIL_ADD":
        left, right = getattr(expr, "left", None), getattr(expr, "right", None)
        if _op(left) in _CONST_OPS and _op(right) not in _CONST_OPS:
            value = left.constant
            return value if isinstance(value, int) else None
        if _op(right) in _CONST_OPS and _op(left) not in _CONST_OPS:
            value = right.constant
            return value if isinstance(value, int) else None
    if _op(expr) == "MLIL_SUB":
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
            if _op(expr) != "MLIL_AND":
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
        if _op(ins) not in ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD", "MLIL_SET_VAR_SSA"):
            continue
        src = getattr(ins, "src", None)
        if _op(src) != "MLIL_ADD":
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
    init = _init_moduli(il)
    store_m = _store_modulus_offsets(il)
    candidates = rem & init
    if not candidates:
        candidates = rem & store_m
    if not candidates:
        candidates = rem
    ends = _cmp_ne_constants(il)
    best = None
    for modulus in sorted(candidates):
        bounds = [bound for bound in ends if bound > modulus]
        if not bounds:
            continue
        # MLIL loop test is typically `(index + 1) != end_exclusive`.
        end_exclusive = max(bounds)
        length = end_exclusive - modulus
        if length <= 0 or length > 4096:
            continue
        spec = {"key_modulus": modulus, "length": length}
        if modulus in init and modulus in store_m:
            return spec
        if best is None:
            best = spec
    return best


def _recognize_index0_loop_string_decrypt(il):
    """Loop with i from 0: `if (i != length)` and payload at src[M + i]."""
    if not _has_done_flag_store(il) or not _has_byte_crypto_store(il):
        return None
    if _rem_moduli(il):
        return None
    ends = {bound for bound in _cmp_ne_constants(il) if 1 < bound <= 4096}
    if not ends:
        return None
    length = max(ends)
    and_moduli = _and_moduli(il)
    if and_moduli:
        modulus = min(and_moduli)
        return {"key_modulus": modulus, "length": length}
    # Bare key index `i` requires length <= M; M is the fixed payload base offset.
    for modulus in sorted(_payload_const_offsets(il)):
        if 2 <= modulus <= 256 and length <= modulus:
            return {"key_modulus": modulus, "length": length}
    return None


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
    if not matches:
        return None
    return {"key_modulus": max(matches), "length": length}


def _recognize_string_decrypt_function(func, il=None):
    il = il or getattr(func, "mlil", None) or getattr(func, "medium_level_il", None)
    if il is None or len(_parameters(func, il)) < 2:
        return None
    return (
        _recognize_rem_loop_string_decrypt(il)
        or _recognize_index0_loop_string_decrypt(il)
        or _recognize_unrolled_string_decrypt(il)
    )


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
        callee = bv.get_function_at(target & U48)
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
