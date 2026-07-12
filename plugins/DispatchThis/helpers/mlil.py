"""MLIL profile helpers."""

from collections import deque
from functools import cache

from binaryninja import MediumLevelILOperation as M, RegisterValueType

from .memory import read_uint_le


U64 = 0xFFFFFFFFFFFFFFFF
U32 = 0xFFFFFFFF


def _names(*operations):
    """Expose the legacy operation-name API without hand-written enum names."""
    return tuple(operation.name for operation in operations)


# Profiles historically exchange operation names so that mixed LLIL/MLIL
# matchers cannot confuse equal IntEnum values. Exact names still come from BN's
# enum and are consumed only by explicit compatibility selectors.
CALL_OPERATIONS = (
    M.MLIL_CALL,
    M.MLIL_CALL_SSA,
    M.MLIL_CALL_UNTYPED,
    M.MLIL_CALL_UNTYPED_SSA,
    M.MLIL_TAILCALL,
    M.MLIL_TAILCALL_SSA,
    M.MLIL_TAILCALL_UNTYPED,
    M.MLIL_TAILCALL_UNTYPED_SSA,
)
CALL_OPS = _names(*CALL_OPERATIONS)
CONST_OPERATIONS = (M.MLIL_CONST, M.MLIL_CONST_PTR)
CONST_OPS = _names(*CONST_OPERATIONS)
LOAD_STRUCT_OPERATIONS = (M.MLIL_LOAD_STRUCT, M.MLIL_LOAD_STRUCT_SSA)
LOAD_STRUCT_OPS = _names(*LOAD_STRUCT_OPERATIONS)
LOAD_OPERATIONS = (M.MLIL_LOAD, M.MLIL_LOAD_SSA, *LOAD_STRUCT_OPERATIONS)
LOAD_OPS = _names(*LOAD_OPERATIONS)
SLOT_LOAD_OPERATIONS = LOAD_OPERATIONS
SLOT_LOAD_OPS = _names(*SLOT_LOAD_OPERATIONS)
SET_VAR_OPERATIONS = (M.MLIL_SET_VAR, M.MLIL_SET_VAR_FIELD)
SET_VAR_OPS = _names(*SET_VAR_OPERATIONS)
STORE_OPERATIONS = (
    M.MLIL_STORE,
    M.MLIL_STORE_SSA,
    M.MLIL_STORE_STRUCT,
    M.MLIL_STORE_STRUCT_SSA,
)
STORE_OPS = _names(*STORE_OPERATIONS)
ADDRESS_OF_OPERATIONS = (M.MLIL_ADDRESS_OF, M.MLIL_ADDRESS_OF_FIELD)
ADDRESS_OF_OPS = _names(*ADDRESS_OF_OPERATIONS)

# BN 5.3 omits vars_written only for these field-write instruction classes.
_FIELD_WRITE_OPERATIONS = {M.MLIL_SET_VAR_FIELD, M.MLIL_SET_VAR_ALIASED_FIELD}
_UNKNOWN_MEMORY_EFFECT_OPERATIONS = {
    M.MLIL_SYSCALL,
    M.MLIL_SYSCALL_UNTYPED,
    M.MLIL_INTRINSIC,
    M.MLIL_SYSCALL_SSA,
    M.MLIL_SYSCALL_UNTYPED_SSA,
    M.MLIL_MEMORY_INTRINSIC_OUTPUT_SSA,
    M.MLIL_INTRINSIC_SSA,
    M.MLIL_MEMORY_INTRINSIC_SSA,
    M.MLIL_BP,
    M.MLIL_TRAP,
    M.MLIL_UNIMPL_MEM,
}
_UNMODELED_OPERATIONS = {M.MLIL_UNIMPL, M.MLIL_UNIMPL_MEM}
_CONSTANT_VALUE_TYPES = {
    RegisterValueType.ConstantValue,
    RegisterValueType.ConstantPointerValue,
    RegisterValueType.ImportedAddressValue,
}

_COMPARISONS = {
    M.MLIL_CMP_E: lambda left, right: left == right,
    M.MLIL_CMP_NE: lambda left, right: left != right,
    M.MLIL_CMP_SLT: lambda left, right: left < right,
    M.MLIL_CMP_ULT: lambda left, right: left < right,
    M.MLIL_CMP_SLE: lambda left, right: left <= right,
    M.MLIL_CMP_ULE: lambda left, right: left <= right,
    M.MLIL_CMP_SGE: lambda left, right: left >= right,
    M.MLIL_CMP_UGE: lambda left, right: left >= right,
    M.MLIL_CMP_SGT: lambda left, right: left > right,
    M.MLIL_CMP_UGT: lambda left, right: left > right,
}
_SIGNED_COMPARISONS = {
    M.MLIL_CMP_SLT,
    M.MLIL_CMP_SLE,
    M.MLIL_CMP_SGE,
    M.MLIL_CMP_SGT,
}


def walk_expr(expr):
    if expr is None:
        return []
    return list(expr.traverse(lambda node: node))


def _operation_selectors(ops):
    return (ops,) if isinstance(ops, (str, M)) else tuple(ops)


def _matches_operation(expr, selectors):
    """Match native MLIL enums, retaining names only at the compatibility seam."""
    current = operation(expr)
    return any(
        (isinstance(value, M) and current == value)
        or (isinstance(value, str) and getattr(current, "name", None) == value)
        for value in selectors
    )


def expression_has_operation(expr, ops):
    """Return whether ``expr`` contains any operation in ``ops``."""
    selectors = _operation_selectors(ops)
    return any(_matches_operation(node, selectors) for node in walk_expr(expr))


def expression_or_definitions_have_operation(mlil, expr, ops, max_depth=16):
    """Return whether ``expr`` or followed MLIL_VAR definitions contain ``ops``."""
    selectors = _operation_selectors(ops)
    return any(
        _matches_operation(node, selectors)
        for node in walk_expr_with_defs(mlil, expr, max_depth=max_depth)
    )


def op_name(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def operation(expr):
    """Return the native Binary Ninja MLIL operation enum, or ``None``."""
    return getattr(expr, "operation", None)


def same_var(left, right):
    """Compare variable identity without a display-name fallback."""
    return left == right


def var_from_expr(expr):
    """Return the base variable read by any full or field variable expression."""
    op = operation(expr)
    if op in (M.MLIL_VAR, M.MLIL_VAR_FIELD):
        return expr.src
    if op in (
        M.MLIL_VAR_SSA,
        M.MLIL_VAR_SSA_FIELD,
        M.MLIL_VAR_ALIASED,
        M.MLIL_VAR_ALIASED_FIELD,
    ):
        return getattr(expr.src, "var", expr.src)
    return None


def direct_var_from_expr(expr):
    """Return only an exact, whole-variable read (never a field or split read)."""
    op = operation(expr)
    if op == M.MLIL_VAR:
        return getattr(expr, "src", None)
    if op == M.MLIL_VAR_SSA:
        source = getattr(expr, "src", None)
        return getattr(source, "var", source)
    return None


def _mask(size):
    return (1 << ((size or 4) * 8)) - 1


def _expression_mask(expr):
    return _mask(getattr(expr, "size", None) or 8)


def state_token(const_expr, fallback_size=None):
    size = getattr(const_expr, "size", None) or fallback_size
    if size is None:
        value = getattr(const_expr, "constant", 0)
        size = 8 if value > U32 or value < 0 else 4
    return (const_expr.constant & _mask(size), size)


def comparison_parts(condition):
    """Return one variable/constant MLIL comparison without losing operand order."""
    op = operation(condition)
    if op not in _COMPARISONS:
        return None
    left_var = direct_var_from_expr(getattr(condition, "left", None))
    right_var = direct_var_from_expr(getattr(condition, "right", None))
    if left_var is not None and operation(condition.right) == M.MLIL_CONST:
        return {
            "op": op,
            "var": left_var,
            "bound": state_token(condition.right),
            "var_on_left": True,
        }
    if right_var is not None and operation(condition.left) == M.MLIL_CONST:
        return {
            "op": op,
            "var": right_var,
            "bound": state_token(condition.left),
            "var_on_left": False,
        }
    return None


def addressed_var(expr):
    """Return the variable named by ADDRESS_OF or ADDRESS_OF_FIELD."""
    return (
        getattr(expr, "src", None)
        if operation(expr) in ADDRESS_OF_OPERATIONS
        else None
    )


def _base_var(var):
    return getattr(var, "var", var)


def instruction_writes_variable(instruction, variable):
    """Return whether any full, partial, split, or aliased write names ``variable``."""
    candidates = list(getattr(instruction, "vars_written", ()) or ())
    if operation(instruction) in _FIELD_WRITE_OPERATIONS:
        candidates.append(getattr(instruction, "dest", None))
    return any(
        candidate is not None and same_var(_base_var(candidate), variable)
        for candidate in candidates
    )


def _read_variables(instruction):
    variables = getattr(instruction, "vars_read", None)
    if variables is not None:
        yield from (_base_var(variable) for variable in variables)
        return
    # Lightweight tests and external profile adapters may expose only the IL
    # tree. Real BN instructions always take the vars_read path above.
    for node in walk_expr(instruction):
        node_variables = getattr(node, "vars_read", None)
        if node_variables is not None:
            yield from (_base_var(variable) for variable in node_variables)
            continue
        variable = var_from_expr(node)
        if variable is not None:
            yield _base_var(variable)


def instruction_reads_variable(instruction, variable):
    """Return whether an instruction reads a full, field, split, or aliased variable."""
    return any(same_var(candidate, variable) for candidate in _read_variables(instruction))


def _addressed_variables_from_expressions(mlil, expressions):
    """Return every address source reachable from the expression roots.

    ``None`` means a definition lookup was incomplete, so callers must treat any
    queried variable as possibly addressed.
    """
    expressions = deque(expressions)
    seen_expressions = set()
    seen_expression_refs = []
    seen_variables = []
    addressed_result = []
    while expressions:
        current = expressions.popleft()
        if current is None:
            continue
        expression_key = id(current)
        if expression_key in seen_expressions:
            continue
        seen_expressions.add(expression_key)
        # BN may return fresh Python wrappers for IL nodes. Keep visited
        # wrappers alive so CPython cannot reuse an id for a different node.
        seen_expression_refs.append(current)

        addressed_variables = list(
            getattr(current, "vars_address_taken", ()) or ()
        )
        # BN 5.3 omits vars_address_taken for MLIL_ADDRESS_OF_FIELD. The
        # per-node fallback also supports lightweight external/test adapters.
        for node in walk_expr(current):
            addressed = addressed_var(node)
            if addressed is not None and not any(
                same_var(_base_var(addressed), _base_var(candidate))
                for candidate in addressed_variables
            ):
                addressed_variables.append(addressed)
        for addressed in map(_base_var, addressed_variables):
            if not any(same_var(addressed, known) for known in addressed_result):
                addressed_result.append(addressed)

        candidates = [*_read_variables(current), *map(_base_var, addressed_variables)]
        for candidate in candidates:
            if candidate is None:
                continue
            if any(same_var(candidate, seen) for seen in seen_variables):
                continue
            seen_variables.append(candidate)
            try:
                definitions = list(mlil.get_var_definitions(candidate))
            except Exception:  # noqa: BLE001
                return None
            expressions.extend(definitions)
    return addressed_result


def _expressions_may_address_variable(mlil, expressions, variable):
    """Conservatively test whether roots may contain an address of ``variable``."""
    addressed = _addressed_variables_from_expressions(mlil, expressions)
    return addressed is None or any(
        same_var(candidate, _base_var(variable)) for candidate in addressed
    )


def expression_may_address_variable(mlil, expression, variable):
    """Follow one expression's definitions looking for an address of ``variable``."""
    return _expressions_may_address_variable(mlil, (expression,), variable)


def _memory_effect_expressions(mlil):
    instructions = getattr(mlil, "instructions", None)
    if instructions is None:
        instructions = (
            instruction
            for block in getattr(mlil, "basic_blocks", ()) or ()
            for instruction in block
        )
    for instruction in instructions or ():
        if operation(instruction) in STORE_OPERATIONS:
            yield getattr(instruction, "src", None)
        elif has_unknown_memory_effect(instruction):
            yield instruction


def address_escape_checker(mlil):
    """Build one current-MLIL address-escape query with shared alias work."""
    uncomputed = object()
    addressed = uncomputed

    @cache
    def cached_address_escapes(variable):
        nonlocal addressed
        if addressed is uncomputed:
            addressed = _addressed_variables_from_expressions(
                mlil,
                _memory_effect_expressions(mlil),
            )
        return addressed is None or any(
            same_var(candidate, variable) for candidate in addressed
        )

    def address_escapes(variable):
        return cached_address_escapes(_base_var(variable))

    return address_escapes


def variable_address_escapes(mlil, variable):
    """Return whether a store or unknown memory effect can retain an address."""
    return any(
        expression_may_address_variable(mlil, expression, variable)
        for expression in _memory_effect_expressions(mlil)
    )


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
        or operation(current) != operation(expected)
    ):
        return None
    for name in ("expr_index", "address"):
        expected_value = getattr(expected, name, None)
        if expected_value is not None and getattr(current, name, None) != expected_value:
            return None
    return current


def has_unknown_memory_effect(instruction):
    """Return whether an instruction may mutate memory outside explicit STORE handling."""
    op = operation(instruction)
    return op in CALL_OPERATIONS or op in _UNKNOWN_MEMORY_EFFECT_OPERATIONS


def has_unmodeled_semantics(instruction):
    """Return whether Binary Ninja could not model an instruction's semantics."""
    return expression_has_operation(instruction, _UNMODELED_OPERATIONS)


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
        operation(definition) != M.MLIL_SET_VAR
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


def region_until(start_bb, stop_starts):
    """Return blocks reachable from ``start_bb`` before any stop block."""
    region = set()
    queue = deque((start_bb,))
    while queue:
        block = queue.popleft()
        if block.start in region or block.start in stop_starts:
            continue
        region.add(block.start)
        queue.extend(
            edge.target
            for edge in block.outgoing_edges
            if edge.target.start not in region and edge.target.start not in stop_starts
        )
    return region


def dependency_variables(mlil, expressions, scope):
    """Return variables on in-scope definition chains rooted at ``expressions``."""
    required = set()
    queue = deque(
        var
        for expression in expressions
        for var in _read_variables(expression)
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
            queue.extend(_read_variables(getattr(definition, "src", None)))
    return required


def scope_locality_checker(mlil):
    """Build one current-MLIL index of variable read/address-taken blocks."""
    uncomputed = object()
    use_blocks = uncomputed

    def variables_are_local(variables, scope):
        nonlocal use_blocks
        if use_blocks is uncomputed:
            indexed_use_blocks = {}
            for block in mlil.basic_blocks:
                block_variables = []
                for instruction in block:
                    block_variables.extend(_read_variables(instruction))
                    block_variables.extend(
                        _base_var(addressed)
                        for expression in walk_expr(instruction)
                        for addressed in (addressed_var(expression),)
                        if addressed is not None
                    )
                for variable in block_variables:
                    indexed_use_blocks.setdefault(variable, set()).add(block.start)
            use_blocks = indexed_use_blocks
        scope = set(scope)
        return all(
            use_blocks.get(_base_var(variable), set()) <= scope
            for variable in variables
        )

    return variables_are_local


def variables_are_scope_local(mlil, variables, scope):
    """Return whether ``variables`` have no reads or address escapes outside ``scope``."""
    variables = tuple(variables)
    for block in mlil.basic_blocks:
        if block.start in scope:
            continue
        for instruction in block:
            if any(
                instruction_reads_variable(instruction, variable)
                for variable in variables
            ):
                return False
            for expression in walk_expr(instruction):
                addressed = addressed_var(expression)
                if addressed is not None and any(
                    same_var(addressed, variable) for variable in variables
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
            if operation(ins) == M.MLIL_SET_VAR and ins.dest in tracked
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
            for var in _read_variables(ins):
                if var in tracked and var not in defined:
                    return False
            if operation(ins) == M.MLIL_SET_VAR and ins.dest in tracked:
                defined.add(ins.dest)
    return True


def _mask_address(value, address_mask):
    return value if address_mask is None else value & address_mask


def constant_value(mlil, expr):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
    )
    return expr.constant if operation(expr) in CONST_OPERATIONS else None


def expression_scalar_value(mlil, expr):
    """Return a direct MLIL constant or Binary Ninja single-value result."""
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
    )
    if operation(expr) in CONST_OPERATIONS:
        return expr.constant
    value = getattr(expr, "value", None)
    if getattr(value, "type", None) in _CONSTANT_VALUE_TYPES:
        return value.value
    return None


def constant_address(mlil, expr, depth=0, max_depth=32, address_mask=None):
    if expr is None or depth > max_depth:
        return None
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=max_depth,
    )
    value = constant_value(mlil, expr)
    if value is not None:
        return _mask_address(value, address_mask)
    op = operation(expr)
    if op in (M.MLIL_ADD, M.MLIL_SUB):
        left = constant_address(mlil, expr.left, depth + 1, max_depth, address_mask)
        right = constant_address(mlil, expr.right, depth + 1, max_depth, address_mask)
        if left is not None and right is not None:
            return _mask_address(
                left + right if op == M.MLIL_ADD else left - right,
                address_mask,
            )
    return None


def load_slot_address(mlil, expr, width=8, address_mask=None):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
    )
    op = operation(expr)
    if op not in SLOT_LOAD_OPERATIONS or getattr(expr, "size", None) != width:
        return None
    addr = constant_address(mlil, expr.src, address_mask=address_mask)
    if addr is None:
        return None
    if op in LOAD_STRUCT_OPERATIONS:
        offset = getattr(expr, "offset", 0)
        if not isinstance(offset, int):
            return None
        return _mask_address(addr + offset, address_mask)
    return addr


def mlil_stores_to_address(mlil, addr, address_mask=None):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            op = operation(expr)
            if op not in STORE_OPERATIONS:
                continue
            destination = constant_address(
                mlil,
                getattr(expr, "dest", None),
                address_mask=address_mask,
            )
            if op in (M.MLIL_STORE_STRUCT, M.MLIL_STORE_STRUCT_SSA):
                offset = getattr(expr, "offset", None)
                if destination is None or type(offset) is not int:
                    continue
                destination = _mask_address(destination + offset, address_mask)
            if destination == addr:
                return True
    return False


def slot_has_no_stores(bv, current_mlil, slot_addr, address_mask=None):
    """Prove that every currently known code reference has analyzed no write."""
    if current_mlil is None:
        return False
    data_var = bv.get_data_var_at(slot_addr)
    if data_var is None or mlil_stores_to_address(
        current_mlil,
        slot_addr,
        address_mask=address_mask,
    ):
        return False

    try:
        refs = list(getattr(data_var, "code_refs", ()) or ())
    except Exception:  # noqa: BLE001
        return False
    current_func = getattr(current_mlil, "source_function", None)
    current_start = getattr(current_func, "start", None)
    seen = set()
    for ref in refs:
        func = getattr(ref, "function", None)
        if func is not None:
            funcs = [func]
        else:
            try:
                funcs = list(bv.get_functions_containing(ref.address))
            except Exception:  # noqa: BLE001
                return False
        if not funcs:
            return False
        for func in funcs:
            key = getattr(func, "start", id(func))
            if key in seen:
                continue
            seen.add(key)
            same_current_function = func is current_func or (
                current_start is not None and getattr(func, "start", None) == current_start
            )
            candidate = current_mlil if same_current_function else getattr(func, "mlil", None)
            if candidate is None or (
                candidate is not current_mlil
                and mlil_stores_to_address(
                    candidate,
                    slot_addr,
                    address_mask=address_mask,
                )
            ):
                return False
    return True


def walk_expr_with_defs(mlil, expr, max_depth=16):
    """Yield expression nodes, following MLIL_VAR definitions."""
    yield from _walk_expr_with_defs(mlil, expr, set(), [], 0, max_depth)


def _walk_expr_with_defs(mlil, expr, seen_exprs, seen_vars, depth, max_depth):
    if expr is None:
        return
    for child in walk_expr(expr):
        expr_key = id(child)
        if expr_key in seen_exprs:
            continue
        seen_exprs.add(expr_key)
        yield child
        if operation(child) != M.MLIL_VAR or mlil is None or depth >= max_depth:
            continue
        variable = getattr(child, "src", None)
        if any(same_var(variable, seen) for seen in seen_vars):
            continue
        seen_vars.append(variable)
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
    )
    slot_addr = load_slot_address(mlil, expr, width=width, address_mask=address_mask)
    if slot_addr is not None:
        return [(slot_addr, 0)]

    op = operation(expr)
    if op not in (M.MLIL_ADD, M.MLIL_SUB):
        return []

    out = []
    right_const = constant_value(mlil, expr.right)
    if right_const is not None:
        addend = _signed_offset(right_const)
        if op == M.MLIL_SUB:
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

    if op == M.MLIL_ADD:
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
        if insn.operation not in CALL_OPERATIONS:
            continue
        if insn.dest.operation in CONST_OPERATIONS:
            continue
        yield insn


def iter_calls(mlil, ops=CALL_OPERATIONS):
    """Yield MLIL call-like instructions."""
    selectors = _operation_selectors(ops)
    for insn in getattr(mlil, "instructions", ()) or ():
        if _matches_operation(insn, selectors):
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
):
    """Follow only unique whole-variable MLIL definitions.

    Multiple reaching definitions and field writes are semantic boundaries, not
    candidates from which a caller may select one representative.
    """
    for _ in range(max_depth):
        if expr is None or expr.operation != M.MLIL_VAR:
            break
        try:
            definitions = list(mlil.get_var_definitions(expr.src) or ())
        except Exception:  # noqa: BLE001
            break
        if len(definitions) != 1:
            break
        definition = definitions[0]
        if definition.operation != M.MLIL_SET_VAR:
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
    if value.type in _CONSTANT_VALUE_TYPES:
        return value.value & _expression_mask(expr)
    return None


def fold_constant_value(bv, mlil, expr, depth=0, max_depth=32, load_address_mask=None):
    """Fold one value, accepting multiple definitions only by consensus."""
    if expr is None or depth > max_depth:
        return None
    op = expr.operation

    if op in CONST_OPERATIONS:
        return expr.constant & _expression_mask(expr)

    native_value = _single_value(expr)
    if native_value is not None:
        return native_value

    if op == M.MLIL_VAR:
        try:
            definitions = list(mlil.get_var_definitions(expr.src) or ())
        except Exception:  # noqa: BLE001
            definitions = []
        if not definitions or any(
            definition.operation != M.MLIL_SET_VAR or not hasattr(definition, "src")
            for definition in definitions
        ):
            return None
        values = [
            fold_constant_value(
                bv,
                mlil,
                definition.src,
                depth + 1,
                max_depth,
                load_address_mask,
            )
            for definition in definitions
        ]
        return values[0] if values[0] is not None and all(
            value == values[0] for value in values[1:]
        ) else None

    if op in (M.MLIL_ADD, M.MLIL_SUB):
        left = fold_constant_value(
            bv, mlil, expr.left, depth + 1, max_depth, load_address_mask
        )
        right = fold_constant_value(
            bv, mlil, expr.right, depth + 1, max_depth, load_address_mask
        )
        if left is None or right is None:
            return None
        return (left + right if op == M.MLIL_ADD else left - right) & _expression_mask(expr)

    if op == M.MLIL_MUL:
        left = fold_constant_value(
            bv, mlil, expr.left, depth + 1, max_depth, load_address_mask
        )
        right = fold_constant_value(
            bv, mlil, expr.right, depth + 1, max_depth, load_address_mask
        )
        return (
            None
            if left is None or right is None
            else (left * right) & _expression_mask(expr)
        )

    if op in (M.MLIL_ZX, M.MLIL_SX, M.MLIL_LOW_PART):
        source = fold_constant_value(
            bv,
            mlil,
            expr.src,
            depth + 1,
            max_depth,
            load_address_mask,
        )
        if source is None:
            return None
        result_bits = min((getattr(expr, "size", None) or 8) * 8, 64)
        result_mask = (1 << result_bits) - 1
        if op != M.MLIL_SX:
            return source & result_mask
        source_bits = min((getattr(expr.src, "size", None) or 8) * 8, 64)
        source_mask = (1 << source_bits) - 1
        source &= source_mask
        sign_bit = 1 << (source_bits - 1)
        signed = source - (1 << source_bits) if source & sign_bit else source
        return signed & result_mask

    if op in LOAD_OPERATIONS:
        addr = fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth, load_address_mask)
        if addr is None:
            return None
        if op in LOAD_STRUCT_OPERATIONS:
            offset = getattr(expr, "offset", None)
            if type(offset) is not int:
                return None
            addr += offset
        return read_uint_le(bv, _mask_address(addr, load_address_mask), expr.size)

    return _single_value(expr)


def cleanup_roots_for_expr(mlil, expr):
    """Instruction indices defining MLIL vars read by ``expr``."""
    roots = set()
    for node in walk_expr(expr):
        if node.operation != M.MLIL_VAR:
            continue
        try:
            defs = mlil.get_var_definitions(node.src)
        except Exception:  # noqa: BLE001
            continue
        for definition in defs:
            if definition.operation in SET_VAR_OPERATIONS:
                roots.add(definition.instr_index)
    return roots


def _assignment_roots_before(block_instrs, position):
    roots = set()
    for previous in reversed(block_instrs[:position]):
        if previous.operation not in SET_VAR_OPERATIONS:
            break
        roots.add(previous.instr_index)
    return roots


def set_roots_before_instruction(mlil, instruction):
    """Contiguous assignment indices immediately before one exact instruction."""
    block = getattr(instruction, "il_basic_block", None)
    instr_index = getattr(instruction, "instr_index", None)
    if block is None or instr_index is None:
        return set()
    block_instrs = [mlil[i] for i in range(block.start, block.end)]
    positions = [
        pos
        for pos, candidate in enumerate(block_instrs)
        if getattr(candidate, "instr_index", None) == instr_index
    ]
    return (
        _assignment_roots_before(block_instrs, positions[0])
        if len(positions) == 1
        else set()
    )


def set_roots_before(mlil, site_addrs):
    """Contiguous assignment indices before all instructions at owned sites."""
    site_addrs = set(site_addrs or ())
    roots = set()
    if mlil is None or not site_addrs:
        return roots

    for block in mlil.basic_blocks:
        block_instrs = [mlil[i] for i in range(block.start, block.end)]
        for position, ins in enumerate(block_instrs):
            if ins.address not in site_addrs:
                continue
            roots.update(_assignment_roots_before(block_instrs, position))
    return roots


__all__ = (
    "ADDRESS_OF_OPERATIONS",
    "ADDRESS_OF_OPS",
    "CALL_OPS",
    "CALL_OPERATIONS",
    "CONST_OPS",
    "CONST_OPERATIONS",
    "LOAD_STRUCT_OPS",
    "LOAD_STRUCT_OPERATIONS",
    "LOAD_OPS",
    "LOAD_OPERATIONS",
    "SET_VAR_OPS",
    "SET_VAR_OPERATIONS",
    "SLOT_LOAD_OPERATIONS",
    "SLOT_LOAD_OPS",
    "STORE_OPS",
    "STORE_OPERATIONS",
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
    "operation",
    "peel_var_definitions",
    "region_until",
    "row_local_copy_chain",
    "same_var",
    "set_roots_before",
    "set_roots_before_instruction",
    "slot_has_no_stores",
    "state_token",
    "variables_are_scope_local",
    "variable_address_escapes",
    "var_from_expr",
    "walk_expr",
    "walk_expr_with_defs",
)
