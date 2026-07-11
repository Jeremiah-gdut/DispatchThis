"""MLIL profile helpers."""

from collections import deque

from .memory import read_uint_le


U64 = 0xFFFFFFFFFFFFFFFF
U32 = 0xFFFFFFFF

CALL_OPS = (
    "MLIL_CALL",
    "MLIL_CALL_SSA",
    "MLIL_CALL_UNTYPED",
    "MLIL_CALL_UNTYPED_SSA",
    "MLIL_TAILCALL",
    "MLIL_TAILCALL_SSA",
    "MLIL_TAILCALL_UNTYPED",
    "MLIL_TAILCALL_UNTYPED_SSA",
)
CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
LOAD_STRUCT_OPS = ("MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")
LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", *LOAD_STRUCT_OPS)
SLOT_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", *LOAD_STRUCT_OPS)
SET_VAR_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")
STORE_OPS = ("MLIL_STORE", "MLIL_STORE_SSA", "MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA")
ADDRESS_OF_OPS = ("MLIL_ADDRESS_OF", "MLIL_ADDRESS_OF_FIELD")

_DEST_WRITE_OPS = {
    "MLIL_SET_VAR",
    "MLIL_SET_VAR_FIELD",
    "MLIL_SET_VAR_SSA",
    "MLIL_SET_VAR_SSA_FIELD",
    "MLIL_SET_VAR_ALIASED",
    "MLIL_SET_VAR_ALIASED_FIELD",
}
_SPLIT_WRITE_OPS = {"MLIL_SET_VAR_SPLIT", "MLIL_SET_VAR_SPLIT_SSA"}
_SPLIT_READ_OPS = {"MLIL_VAR_SPLIT", "MLIL_VAR_SPLIT_SSA"}
_UNKNOWN_MEMORY_EFFECT_PREFIXES = (
    "MLIL_SYSCALL",
    "MLIL_INTRINSIC",
    "MLIL_MEMORY_INTRINSIC",
)
_UNKNOWN_MEMORY_EFFECT_OPS = {"MLIL_BP", "MLIL_TRAP", "MLIL_UNIMPL_MEM"}
_UNMODELED_OPS = {"MLIL_UNIMPL", "MLIL_UNIMPL_MEM"}

_COMPARISONS = {
    "MLIL_CMP_E": lambda left, right: left == right,
    "MLIL_CMP_NE": lambda left, right: left != right,
    "MLIL_CMP_SLT": lambda left, right: left < right,
    "MLIL_CMP_ULT": lambda left, right: left < right,
    "MLIL_CMP_SLE": lambda left, right: left <= right,
    "MLIL_CMP_ULE": lambda left, right: left <= right,
    "MLIL_CMP_SGE": lambda left, right: left >= right,
    "MLIL_CMP_UGE": lambda left, right: left >= right,
    "MLIL_CMP_SGT": lambda left, right: left > right,
    "MLIL_CMP_UGT": lambda left, right: left > right,
}
_SIGNED_COMPARISONS = {
    "MLIL_CMP_SLT",
    "MLIL_CMP_SLE",
    "MLIL_CMP_SGE",
    "MLIL_CMP_SGT",
}


def walk_expr(expr):
    if expr is None:
        return []
    try:
        return list(expr.traverse(lambda node: node))
    except AttributeError:
        pass

    out = []
    seen = set()

    def visit(node):
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        out.append(node)
        for name in ("src", "dest", "left", "right", "condition"):
            child = getattr(node, name, None)
            if hasattr(child, "operation"):
                visit(child)
        for name in ("params", "output", "vars_read", "vars_written"):
            for child in getattr(node, name, ()) or ():
                if hasattr(child, "operation"):
                    visit(child)

    visit(expr)
    return out


def _op_names(ops):
    return {ops} if isinstance(ops, str) else set(ops)


def expression_has_operation(expr, ops):
    """Return whether ``expr`` contains any operation in ``ops``."""
    wanted = _op_names(ops)
    return any(op_name(node) in wanted for node in walk_expr(expr))


def expression_or_definitions_have_operation(mlil, expr, ops, max_depth=16):
    """Return whether ``expr`` or followed MLIL_VAR definitions contain ``ops``."""
    wanted = _op_names(ops)
    return any(
        op_name(node) in wanted
        for node in walk_expr_with_defs(mlil, expr, max_depth=max_depth)
    )


def op_name(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


_op_name = op_name


def same_var(left, right):
    """Compare variable identity without a display-name fallback."""
    return left == right


def var_from_expr(expr):
    """Return the base variable read by any full or field variable expression."""
    op = op_name(expr)
    if op in ("MLIL_VAR", "MLIL_VAR_FIELD"):
        return expr.src
    if op in (
        "MLIL_VAR_SSA",
        "MLIL_VAR_SSA_FIELD",
        "MLIL_VAR_FIELD_SSA",
        "MLIL_VAR_ALIASED",
        "MLIL_VAR_ALIASED_FIELD",
    ):
        return getattr(expr.src, "var", expr.src)
    return None


def direct_var_from_expr(expr):
    """Return only an exact, whole-variable read (never a field or split read)."""
    operation = op_name(expr)
    if operation == "MLIL_VAR":
        return getattr(expr, "src", None)
    if operation == "MLIL_VAR_SSA":
        source = getattr(expr, "src", None)
        return getattr(source, "var", source)
    return None


def _mask(size):
    return (1 << ((size or 4) * 8)) - 1


def state_token(const_expr, fallback_size=None):
    size = getattr(const_expr, "size", None) or fallback_size
    if size is None:
        value = getattr(const_expr, "constant", 0)
        size = 8 if value > U32 or value < 0 else 4
    return (const_expr.constant & _mask(size), size)


def comparison_parts(condition):
    """Return one variable/constant MLIL comparison without losing operand order."""
    operation = op_name(condition)
    if operation not in _COMPARISONS:
        return None
    left_var = direct_var_from_expr(getattr(condition, "left", None))
    right_var = direct_var_from_expr(getattr(condition, "right", None))
    if left_var is not None and op_name(condition.right) == "MLIL_CONST":
        return {
            "op": operation,
            "var": left_var,
            "bound": state_token(condition.right),
            "var_on_left": True,
        }
    if right_var is not None and op_name(condition.left) == "MLIL_CONST":
        return {
            "op": operation,
            "var": right_var,
            "bound": state_token(condition.left),
            "var_on_left": False,
        }
    return None


def addressed_var(expr):
    """Return the variable named by ADDRESS_OF or ADDRESS_OF_FIELD."""
    return getattr(expr, "src", None) if op_name(expr) in ADDRESS_OF_OPS else None


def _base_var(var):
    return getattr(var, "var", var)


def instruction_writes_variable(instruction, variable):
    """Return whether any full, partial, split, or aliased write names ``variable``."""
    candidates = list(getattr(instruction, "vars_written", ()) or ())
    operation = op_name(instruction)
    if operation in _DEST_WRITE_OPS:
        candidates.extend(
            getattr(instruction, name, None)
            for name in ("dest", "prev")
        )
    elif operation in _SPLIT_WRITE_OPS:
        candidates.extend(
            getattr(instruction, name, None)
            for name in ("high", "low")
        )
    return any(
        candidate is not None and same_var(_base_var(candidate), variable)
        for candidate in candidates
    )


def _node_read_variables(node):
    variable = var_from_expr(node)
    if variable is not None:
        yield _base_var(variable)
    for candidate in getattr(node, "vars_read", ()) or ():
        yield _base_var(candidate)
    if op_name(node) in _SPLIT_READ_OPS:
        for name in ("high", "low"):
            candidate = getattr(node, name, None)
            if candidate is not None:
                yield _base_var(candidate)


def _read_variables(instruction):
    for node in walk_expr(instruction):
        yield from _node_read_variables(node)


def instruction_reads_variable(instruction, variable):
    """Return whether an instruction reads a full, field, split, or aliased variable."""
    return any(same_var(candidate, variable) for candidate in _read_variables(instruction))


def expression_may_address_variable(mlil, expression, variable):
    """Follow all variable definitions looking for an address of ``variable``.

    This is a conservative may-alias query: an incomplete definition lookup is
    treated as a possible alias so callers that rewrite control flow fail closed.
    """
    expressions = deque((expression,))
    seen_expressions = set()
    seen_variables = []
    while expressions:
        current = expressions.popleft()
        if current is None:
            continue
        for node in walk_expr(current):
            expression_key = id(node)
            if expression_key in seen_expressions:
                continue
            seen_expressions.add(expression_key)
            addressed = addressed_var(node)
            if addressed is not None and same_var(_base_var(addressed), variable):
                return True
            candidates = list(_node_read_variables(node))
            if addressed is not None:
                candidates.append(_base_var(addressed))
            for candidate in candidates:
                if any(same_var(candidate, seen) for seen in seen_variables):
                    continue
                seen_variables.append(candidate)
                try:
                    definitions = list(mlil.get_var_definitions(candidate))
                except Exception:  # noqa: BLE001
                    return True
                expressions.extend(definitions)
    return False


def variable_address_escapes(mlil, variable):
    """Return whether a store or unknown memory effect can retain an address."""
    instructions = getattr(mlil, "instructions", None)
    if instructions is None:
        instructions = (
            instruction
            for block in getattr(mlil, "basic_blocks", ()) or ()
            for instruction in block
        )
    for instruction in instructions or ():
        operation = op_name(instruction)
        if operation in STORE_OPS:
            expression = getattr(instruction, "src", None)
        elif has_unknown_memory_effect(instruction):
            expression = instruction
        else:
            continue
        if expression_may_address_variable(mlil, expression, variable):
            return True
    return False


def current_non_ssa_instruction(mlil, ssa_instruction):
    """Map an SSA instruction to the exact current non-SSA MLIL instruction."""
    expected = getattr(ssa_instruction, "non_ssa_form", None)
    if expected is None:
        expected = ssa_instruction
    instr_index = getattr(expected, "instr_index", None)
    if type(instr_index) is not int or instr_index < 0:
        return None
    try:
        current = mlil[instr_index]
    except Exception:  # noqa: BLE001
        return None
    if (
        getattr(current, "instr_index", None) != instr_index
        or op_name(current) != op_name(expected)
    ):
        return None
    for name in ("expr_index", "address"):
        expected_value = getattr(expected, name, None)
        if expected_value is not None and getattr(current, name, None) != expected_value:
            return None
    return current


def has_unknown_memory_effect(instruction):
    """Return whether an instruction may mutate memory outside explicit STORE handling."""
    operation = op_name(instruction) or ""
    return (
        operation in CALL_OPS
        or operation in _UNKNOWN_MEMORY_EFFECT_OPS
        or operation.startswith(_UNKNOWN_MEMORY_EFFECT_PREFIXES)
    )


def has_unmodeled_semantics(instruction):
    """Return whether Binary Ninja could not model an instruction's semantics."""
    return expression_has_operation(instruction, _UNMODELED_OPS)


def evaluate_comparison(parts, token):
    """Evaluate a concrete state token with Binary Ninja comparison semantics."""
    bound = parts["bound"]
    if token[1] != bound[1]:
        return None
    state_value, bound_value = token[0], bound[0]
    if parts["op"] in _SIGNED_COMPARISONS:
        sign = 1 << (token[1] * 8 - 1)
        state_value = state_value - (sign << 1) if state_value & sign else state_value
        bound_value = bound_value - (sign << 1) if bound_value & sign else bound_value
    left, right = (
        (state_value, bound_value)
        if parts["var_on_left"]
        else (bound_value, state_value)
    )
    return _COMPARISONS[parts["op"]](left, right)


def row_local_copy_chain(mlil, variable, row, use, seen=()):
    """Return a safe row-local copy chain ending at its dispatcher input."""
    if any(same_var(variable, prior) for prior in seen):
        return None
    try:
        definitions = list(mlil.get_var_definitions(variable))
    except Exception:  # noqa: BLE001
        return None
    row_definitions = [
        definition
        for definition in definitions
        if getattr(definition, "il_basic_block", None) is not None
        and definition.il_basic_block.start == row.start
    ]
    if not row_definitions:
        return (variable,)
    if len(row_definitions) != 1:
        return None
    definition = row_definitions[0]
    definition_block = getattr(definition, "il_basic_block", None)
    if (
        op_name(definition) != "MLIL_SET_VAR"
        or definition_block is None
        or definition_block.start != row.start
        or getattr(definition, "instr_index", None) is None
        or getattr(use, "instr_index", None) is None
        or definition.instr_index >= use.instr_index
    ):
        return None
    source = getattr(definition, "src", None)
    source_var = direct_var_from_expr(source)
    if source_var is None:
        return None
    definition_size = getattr(definition, "size", None)
    source_size = getattr(source, "size", None)
    if (
        definition_size is not None
        and source_size is not None
        and definition_size != source_size
    ):
        return None
    tail = row_local_copy_chain(
        mlil,
        source_var,
        row,
        definition,
        (*seen, variable),
    )
    return None if tail is None else (variable, *tail)


def all_paths_reach_stops(basic_blocks, scope, stop_starts):
    """Return whether every path through ``scope`` terminates in ``stop_starts``."""
    scope = set(scope)
    stop_starts = set(stop_starts)
    scoped = {bb.start: bb for bb in basic_blocks if bb.start in scope}
    if set(scoped) != scope:
        return False

    proven = set()
    changed = True
    while changed:
        changed = False
        for start, bb in scoped.items():
            if start in proven:
                continue
            successors = {edge.target.start for edge in bb.outgoing_edges}
            if not successors:
                continue
            if any(target not in scope and target not in stop_starts for target in successors):
                continue
            if all(target in stop_starts or target in proven for target in successors):
                proven.add(start)
                changed = True
    return proven == scope


def all_paths_hit_blocks(basic_blocks, starts, scope, hit_starts):
    """Return whether every scoped path from ``starts`` reaches a hit block."""
    scope = set(scope)
    starts = set(starts)
    scoped = {bb.start: bb for bb in basic_blocks if bb.start in scope}
    if not starts or not starts <= set(scoped):
        return False

    proven = set(hit_starts) & scope
    changed = True
    while changed:
        changed = False
        for start, bb in scoped.items():
            if start in proven:
                continue
            successors = {edge.target.start for edge in bb.outgoing_edges}
            if successors and all(target in proven for target in successors):
                proven.add(start)
                changed = True
    return starts <= proven


def dependency_variables(mlil, expressions, scope):
    """Return variables on in-scope definition chains rooted at ``expressions``."""
    required = set()
    queue = deque(
        var
        for expression in expressions
        for node in walk_expr(expression)
        if (var := var_from_expr(node)) is not None
    )
    while queue:
        var = queue.popleft()
        if var in required:
            continue
        required.add(var)
        for definition in mlil.get_var_definitions(var):
            block = getattr(definition, "il_basic_block", None)
            if block is None or block.start not in scope:
                continue
            queue.extend(
                source_var
                for node in walk_expr(getattr(definition, "src", None))
                if (source_var := var_from_expr(node)) is not None
            )
    return required


def transitive_definition_variables(mlil, variables):
    """Return variables reachable through all available definition sources."""
    dependencies = set()
    queue = deque(variables)
    while queue:
        var = queue.popleft()
        if var in dependencies:
            continue
        dependencies.add(var)
        for definition in mlil.get_var_definitions(var):
            queue.extend(
                source_var
                for node in walk_expr(getattr(definition, "src", None))
                if (source_var := var_from_expr(node)) is not None
            )
    return dependencies


def variables_are_scope_local(mlil, variables, scope):
    """Return whether ``variables`` have no reads or address escapes outside ``scope``."""
    variables = tuple(variables)
    for bb in mlil.basic_blocks:
        if bb.start in scope:
            continue
        for ins in bb:
            if any(instruction_reads_variable(ins, var) for var in variables):
                return False
            for expr in walk_expr(ins):
                addressed = addressed_var(expr)
                if addressed is not None and any(
                    same_var(addressed, var) for var in variables
                ):
                    return False
    return True


def definitions_cover_all_paths(mlil, starts, scope, expressions):
    """Prove in-scope dependencies are defined before use on every entry path."""
    scope = set(scope)
    starts = set(starts)
    blocks = {bb.start: bb for bb in mlil.basic_blocks if bb.start in scope}
    if not starts or not starts <= set(blocks):
        return False

    required = dependency_variables(mlil, expressions, scope)
    tracked = {
        var
        for var in required
        if any(
            getattr(definition, "il_basic_block", None) is not None
            and definition.il_basic_block.start in scope
            for definition in mlil.get_var_definitions(var)
        )
    }
    if not tracked:
        return True

    block_defs = {}
    for start, bb in blocks.items():
        block_defs[start] = {
            ins.dest
            for ins in bb
            if op_name(ins) == "MLIL_SET_VAR" and ins.dest in tracked
        }

    incoming = {
        start: set() if start in starts else set(tracked)
        for start in blocks
    }
    outgoing = {
        start: incoming[start] | block_defs[start]
        for start in blocks
    }
    changed = True
    while changed:
        changed = False
        for start, bb in blocks.items():
            predecessors = [
                outgoing[edge.source.start]
                for edge in bb.incoming_edges
                if edge.source.start in scope
            ]
            new_in = (
                set()
                if start in starts or not predecessors
                else set.intersection(*(set(values) for values in predecessors))
            )
            new_out = new_in | block_defs[start]
            if new_in != incoming[start] or new_out != outgoing[start]:
                incoming[start] = new_in
                outgoing[start] = new_out
                changed = True

    for start, bb in blocks.items():
        defined = set(incoming[start])
        for ins in bb:
            for expr in walk_expr(ins):
                var = var_from_expr(expr)
                if var is not None and var in tracked and var not in defined:
                    return False
            if op_name(ins) == "MLIL_SET_VAR" and ins.dest in tracked:
                defined.add(ins.dest)
    return True


def _mask_address(value, address_mask):
    return value if address_mask is None else value & address_mask


def constant_value(mlil, expr):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    return expr.constant if _op_name(expr) in CONST_OPS else None


def expression_scalar_value(mlil, expr):
    """Return a direct MLIL constant or Binary Ninja single-value result."""
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    if _op_name(expr) in CONST_OPS:
        return expr.constant
    value = getattr(expr, "value", None)
    value_type = getattr(getattr(value, "type", None), "name", None)
    if value_type in ("ConstantValue", "ConstantPointerValue", "ImportedAddressValue"):
        return value.value
    return None


def constant_address(mlil, expr, depth=0, max_depth=32, address_mask=None):
    if expr is None or depth > max_depth:
        return None
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=max_depth,
        require_single=True,
        allowed_ops=None,
    )
    value = constant_value(mlil, expr)
    if value is not None:
        return _mask_address(value, address_mask)
    op = _op_name(expr)
    if op in ("MLIL_ADD", "MLIL_SUB"):
        left = constant_address(mlil, expr.left, depth + 1, max_depth, address_mask)
        right = constant_address(mlil, expr.right, depth + 1, max_depth, address_mask)
        if left is not None and right is not None:
            return _mask_address(left + right if op == "MLIL_ADD" else left - right, address_mask)
    return None


def load_slot_address(mlil, expr, width=8, address_mask=None):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    op = _op_name(expr)
    if op not in SLOT_LOAD_OPS or getattr(expr, "size", None) != width:
        return None
    addr = constant_address(mlil, expr.src, address_mask=address_mask)
    if addr is None:
        return None
    if op in LOAD_STRUCT_OPS:
        offset = getattr(expr, "offset", 0)
        if not isinstance(offset, int):
            return None
        return _mask_address(addr + offset, address_mask)
    return addr


def mlil_stores_to_address(mlil, addr, address_mask=None):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if (
                _op_name(expr) in STORE_OPS
                and constant_address(
                    mlil,
                    getattr(expr, "dest", None),
                    address_mask=address_mask,
                ) == addr
            ):
                return True
    return False


def walk_expr_with_defs(mlil, expr, max_depth=16):
    """Yield expression nodes, following MLIL_VAR definitions."""
    yield from _walk_expr_with_defs(mlil, expr, set(), set(), 0, max_depth)


def _walk_expr_with_defs(mlil, expr, seen_exprs, seen_vars, depth, max_depth):
    if expr is None:
        return
    for child in walk_expr(expr):
        expr_key = id(child)
        if expr_key in seen_exprs:
            continue
        seen_exprs.add(expr_key)
        yield child
        if _op_name(child) != "MLIL_VAR" or mlil is None or depth >= max_depth:
            continue
        var_key = repr(getattr(child, "src", None))
        if var_key in seen_vars:
            continue
        seen_vars.add(var_key)
        try:
            definitions = mlil.get_var_definitions(child.src)
        except Exception:  # noqa: BLE001
            continue
        for definition in definitions or ():
            yield from _walk_expr_with_defs(
                mlil,
                getattr(definition, "src", None),
                seen_exprs,
                seen_vars,
                depth + 1,
                max_depth,
            )


def load_slot_offsets(mlil, expr, width=8, address_mask=None, max_depth=32):
    """Return ``(slot_addr, offset)`` pairs for slot loads plus constant offsets."""
    return _load_slot_offsets(mlil, expr, width, address_mask, 0, max_depth)


def iter_load_slot_offsets(mlil, width=8, address_mask=None):
    """Yield ``(expr, use_addr, slot_addr, offset)`` for every slot-offset use."""
    for ins in getattr(mlil, "instructions", ()) or ():
        ins_addr = getattr(ins, "address", 0)
        for expr in walk_expr(ins):
            use_addr = getattr(expr, "address", ins_addr)
            for slot_addr, offset in load_slot_offsets(
                mlil,
                expr,
                width=width,
                address_mask=address_mask,
            ):
                yield expr, use_addr, slot_addr, offset


def _load_slot_offsets(mlil, expr, width, address_mask, depth, max_depth):
    if depth > max_depth:
        return []
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=max_depth,
        require_single=True,
        allowed_ops=None,
    )
    slot_addr = load_slot_address(mlil, expr, width=width, address_mask=address_mask)
    if slot_addr is not None:
        return [(slot_addr, 0)]

    op = _op_name(expr)
    if op not in ("MLIL_ADD", "MLIL_SUB"):
        return []

    out = []
    right_const = constant_value(mlil, expr.right)
    if right_const is not None:
        addend = _signed_offset(right_const)
        if op == "MLIL_SUB":
            addend = -addend
        out.extend(
            (slot, offset + addend)
            for slot, offset in _load_slot_offsets(
                mlil,
                expr.left,
                width,
                address_mask,
                depth + 1,
                max_depth,
            )
        )

    if op == "MLIL_ADD":
        left_const = constant_value(mlil, expr.left)
        if left_const is not None:
            addend = _signed_offset(left_const)
            out.extend(
                (slot, offset + addend)
                for slot, offset in _load_slot_offsets(
                    mlil,
                    expr.right,
                    width,
                    address_mask,
                    depth + 1,
                    max_depth,
                )
            )

    return out


def _signed_offset(value):
    return value - (1 << 64) if value > 0x7FFFFFFFFFFFFFFF else value


def iter_indirect_calls(mlil):
    """Yield MLIL call instructions whose destination is not already direct."""
    if mlil is None:
        return
    for insn in mlil.instructions:
        if not insn.operation.name.startswith("MLIL_CALL"):
            continue
        if insn.dest.operation.name in CONST_OPS:
            continue
        yield insn


def iter_calls(mlil, ops=CALL_OPS):
    """Yield MLIL call-like instructions."""
    wanted = _op_names(ops)
    for insn in getattr(mlil, "instructions", ()) or ():
        if _op_name(insn) in wanted:
            yield insn


def iter_direct_calls(mlil):
    """Yield MLIL call-like instructions with a recoverable scalar destination."""
    for insn in iter_calls(mlil):
        if expression_scalar_value(mlil, getattr(insn, "dest", None)) is not None:
            yield insn


def peel_var_definitions(
    mlil,
    expr,
    trail=None,
    max_depth=64,
    require_single=False,
    allowed_ops=SET_VAR_OPS,
):
    """Follow MLIL_VAR through SET_VAR definitions and return the peeled expr."""
    for _ in range(max_depth):
        if expr is None or expr.operation.name != "MLIL_VAR":
            break
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            break
        if not defs or (require_single and len(defs) != 1):
            break
        definition = defs[0]
        if allowed_ops is not None and definition.operation.name not in allowed_ops:
            break
        if not hasattr(definition, "src"):
            break
        if trail is not None:
            trail.append(definition)
        expr = definition.src
    return expr


def _single_value(expr):
    try:
        value = expr.value
    except Exception:  # noqa: BLE001
        return None
    if value.type.name in ("ConstantValue", "ConstantPointerValue", "ImportedAddressValue"):
        return value.value & U64
    return None


def fold_constant_value(bv, mlil, expr, depth=0, max_depth=32, load_address_mask=None):
    """Best-effort single-value fold for current MLIL call-target recovery."""
    if expr is None or depth > max_depth:
        return None
    op = expr.operation.name

    if op in CONST_OPS:
        return expr.constant & U64

    if op == "MLIL_VAR":
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            defs = ()
        if defs and defs[0].operation.name in SET_VAR_OPS:
            value = fold_constant_value(
                bv, mlil, defs[0].src, depth + 1, max_depth, load_address_mask
            )
            if value is not None:
                return value
        return _single_value(expr)

    if op in ("MLIL_ADD", "MLIL_SUB"):
        left = fold_constant_value(
            bv, mlil, expr.left, depth + 1, max_depth, load_address_mask
        )
        right = fold_constant_value(
            bv, mlil, expr.right, depth + 1, max_depth, load_address_mask
        )
        if left is None or right is None:
            return None
        return (left + right if op == "MLIL_ADD" else left - right) & U64

    if op == "MLIL_MUL":
        left = fold_constant_value(
            bv, mlil, expr.left, depth + 1, max_depth, load_address_mask
        )
        right = fold_constant_value(
            bv, mlil, expr.right, depth + 1, max_depth, load_address_mask
        )
        return None if left is None or right is None else (left * right) & U64

    if op in ("MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"):
        return fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth, load_address_mask)

    if op in LOAD_OPS:
        addr = fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth, load_address_mask)
        if addr is None:
            return None
        return read_uint_le(bv, _mask_address(addr, load_address_mask), expr.size)

    return _single_value(expr)


def cleanup_roots_for_expr(mlil, expr):
    """Instruction indices defining MLIL vars read by ``expr``."""
    roots = set()
    for node in walk_expr(expr):
        if node.operation.name != "MLIL_VAR":
            continue
        try:
            defs = mlil.get_var_definitions(node.src)
        except Exception:  # noqa: BLE001
            continue
        for definition in defs:
            if definition.operation.name in SET_VAR_OPS:
                roots.add(definition.instr_index)
    return roots


def set_roots_before(mlil, site_addrs):
    """Contiguous pure assignment instruction indices before owned sites."""
    site_addrs = set(site_addrs or ())
    roots = set()
    if mlil is None or not site_addrs:
        return roots

    for block in mlil.basic_blocks:
        block_instrs = [mlil[i] for i in range(block.start, block.end)]
        for pos, ins in enumerate(block_instrs):
            if ins.address not in site_addrs:
                continue
            for prev in reversed(block_instrs[:pos]):
                if prev.operation.name not in SET_VAR_OPS:
                    break
                roots.add(prev.instr_index)
    return roots


__all__ = (
    "ADDRESS_OF_OPS",
    "CALL_OPS",
    "CONST_OPS",
    "LOAD_STRUCT_OPS",
    "LOAD_OPS",
    "SET_VAR_OPS",
    "SLOT_LOAD_OPS",
    "STORE_OPS",
    "addressed_var",
    "cleanup_roots_for_expr",
    "comparison_parts",
    "all_paths_reach_stops",
    "all_paths_hit_blocks",
    "constant_address",
    "constant_value",
    "current_non_ssa_instruction",
    "dependency_variables",
    "definitions_cover_all_paths",
    "fold_constant_value",
    "expression_has_operation",
    "expression_may_address_variable",
    "expression_or_definitions_have_operation",
    "expression_scalar_value",
    "evaluate_comparison",
    "direct_var_from_expr",
    "iter_calls",
    "iter_direct_calls",
    "iter_load_slot_offsets",
    "iter_indirect_calls",
    "instruction_writes_variable",
    "instruction_reads_variable",
    "has_unknown_memory_effect",
    "has_unmodeled_semantics",
    "load_slot_offsets",
    "load_slot_address",
    "mlil_stores_to_address",
    "op_name",
    "peel_var_definitions",
    "row_local_copy_chain",
    "same_var",
    "set_roots_before",
    "state_token",
    "transitive_definition_variables",
    "variables_are_scope_local",
    "variable_address_escapes",
    "var_from_expr",
    "walk_expr",
    "walk_expr_with_defs",
)
