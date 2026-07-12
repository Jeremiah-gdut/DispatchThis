from collections import deque

from binaryninja import MediumLevelILOperation as M, TypeClass

from . import valorant_2_6
from ..helpers import facts, memory, mlil
from ..utils.log import log_info, log_warn


PROFILE_ID = "driver_2_6"
PROFILE_NAME = "Driver 2.6"
PROFILE_DESCRIPTION = (
    "Rules for the 2.6 driver binary: Valorant-compatible branch/call "
    "hooks plus driver deflattening and string decrypt."
)

# Supported:
# - branch gadget: alias valorant_2_6
# - indirect call gadget: alias valorant_2_6
# - global constants: custom
# - correlated stores: omitted
# - deflatten: custom
# - string decrypt: custom
#
# Validation:
# - deflatten: main @ 0x36d10, state var_124, state pointer var_168,
#   40 redirection plans recovered in driver.bndb.
# - string decrypt: main @ 0x36d10, 65 decrypt facts recovered in driver.bndb.

_DISPATCHER_MIN_ROWS = 3
_CONST_DATA_SECTIONS = {".data"}
_CONST_PTR_TYPE = "void const* const"
_CONST_QWORD_TYPE = "int64_t const"
_U48 = 0xFFFFFFFFFFFF
_DIRECT_CALL_OPS = (
    M.MLIL_CALL,
    M.MLIL_CALL_UNTYPED,
    M.MLIL_TAILCALL,
)
_COMPARISON_OPERATIONS = {
    M.MLIL_CMP_E,
    M.MLIL_CMP_NE,
    M.MLIL_CMP_SLT,
    M.MLIL_CMP_ULT,
    M.MLIL_CMP_SLE,
    M.MLIL_CMP_ULE,
    M.MLIL_CMP_SGE,
    M.MLIL_CMP_UGE,
    M.MLIL_CMP_SGT,
    M.MLIL_CMP_UGT,
}


resolve_branch_gadget = valorant_2_6.resolve_branch_gadget
resolve_call_gadget = valorant_2_6.resolve_call_gadget


def plan_global_constant_slots(bv, il):
    plans = {}
    for slot_addr in _driver_global_constant_slot_refs(il):
        _add_driver_global_constant_plan(plans, bv, il, slot_addr)
    return [plans[addr] for addr in sorted(plans)]


def _driver_global_constant_slot_refs(il):
    if il is None:
        return []
    refs = set()
    for call in mlil.iter_calls(il, _DIRECT_CALL_OPS):
        params = list(getattr(call, "params", ()) or ())
        if len(params) < 2:
            continue
        for expr in mlil.walk_expr_with_defs(il, params[1], max_depth=32):
            for slot_addr, _offset in mlil.load_slot_offsets(il, expr, address_mask=_U48):
                refs.add(slot_addr)
    return refs


def _add_driver_global_constant_plan(plans, bv, il, slot_addr):
    if slot_addr in plans:
        return
    data_var = bv.get_data_var_at(slot_addr)
    type_ = getattr(data_var, "type", None)
    type_class = getattr(type_, "type_class", None)
    is_void_pointer = (
        type_class == TypeClass.PointerTypeClass
        and getattr(type_, "width", None) == 8
        and getattr(getattr(type_, "target", None), "type_class", None)
        == TypeClass.VoidTypeClass
    )
    is_signed_qword = (
        type_class == TypeClass.IntegerTypeClass
        and getattr(type_, "width", None) == 8
        and bool(getattr(type_, "signed", False))
    )
    if not (is_void_pointer or is_signed_qword):
        return
    if not mlil.slot_has_no_stores(bv, il, slot_addr, address_mask=_U48):
        return
    if not memory.in_section(bv, slot_addr, _CONST_DATA_SECTIONS):
        return
    value = memory.read_qword_slot(bv, slot_addr)
    if value is None:
        return
    type_name = _CONST_PTR_TYPE if is_void_pointer else _CONST_QWORD_TYPE
    plans[slot_addr] = facts.global_constant_fact(slot_addr, type_name)


def plan_string_decrypt_calls(bv, _func, il, _mlil_stable):
    if il is None:
        return []

    out = []
    for call in mlil.iter_calls(il, _DIRECT_CALL_OPS):
        target = mlil.expression_scalar_value(il, getattr(call, "dest", None))
        params = list(getattr(call, "params", ()) or ())
        if target is None or len(params) < 2:
            continue
        dst_addr = mlil.expression_scalar_value(il, params[0])
        src_addr = mlil.expression_scalar_value(il, params[1])
        if dst_addr is None or src_addr is None:
            continue
        callee = bv.get_function_at(target)
        if callee is None:
            continue
        spec = _recognize_driver_string_decrypt_function(callee)
        if spec is None:
            continue
        plaintext = _decode_driver_string_blob(bv, src_addr, spec)
        if plaintext is None:
            log_warn(
                f"[driver_2_6:sdecrypt] {hex(call.address)}: "
                f"source blob @ {hex(src_addr)} is too short for {spec}"
            )
            continue
        out.append(facts.string_decrypt_fact(call.address, src_addr, dst_addr, plaintext))
    return out


def _const_from_binary_expr(expr):
    for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
        if mlil.operation(side) == M.MLIL_CONST:
            return side.constant
    return None


def _divu_constants(il):
    values = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if mlil.operation(expr) != M.MLIL_DIVU:
                continue
            value = _const_from_binary_expr(expr)
            if value is not None:
                values.add(value)
    return values


def _var_defined_as_increment(il, var):
    for definition in il.get_var_definitions(var):
        src = getattr(definition, "src", None)
        if mlil.operation(src) != M.MLIL_ADD:
            continue
        left = getattr(src, "left", None)
        right = getattr(src, "right", None)
        if (
            mlil.direct_var_from_expr(left) is not None
            and mlil.operation(right) == M.MLIL_CONST
            and right.constant == 1
        ) or (
            mlil.direct_var_from_expr(right) is not None
            and mlil.operation(left) == M.MLIL_CONST
            and left.constant == 1
        ):
            return True
    return False


def _length_constants(il):
    values = set()
    for ins in getattr(il, "instructions", ()) or ():
        if mlil.operation(ins) != M.MLIL_IF:
            continue
        cond = getattr(ins, "condition", None)
        if mlil.operation(cond) != M.MLIL_CMP_E:
            continue
        sides = (getattr(cond, "left", None), getattr(cond, "right", None))
        const_expr = next(
            (side for side in sides if mlil.operation(side) == M.MLIL_CONST),
            None,
        )
        var_expr = next((side for side in sides if mlil.var_from_expr(side) is not None), None)
        if const_expr is None or var_expr is None:
            continue
        value = const_expr.constant
        if 1 < value <= 4096 and _var_defined_as_increment(il, mlil.var_from_expr(var_expr)):
            values.add(value)
    return values


def _recognize_driver_string_decrypt_function(func):
    il = getattr(func, "mlil", None) or getattr(func, "medium_level_il", None)
    if il is None:
        return None
    if not _has_driver_decrypt_body(il):
        return None
    divu_values = _divu_constants(il)
    length_values = _length_constants(il)
    if len(divu_values) != 1 or len(length_values) != 1:
        return None
    key_modulus = next(iter(divu_values))
    length = next(iter(length_values))
    if not (0 < key_modulus <= 4096) or length <= 0:
        return None
    return {"key_modulus": key_modulus, "length": length}


def _has_driver_decrypt_body(il):
    byte_loads = 0
    byte_stores = 0
    has_xor = False
    has_mul = False
    has_and = False
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            op = mlil.operation(expr)
            if op in mlil.LOAD_OPERATIONS and getattr(expr, "size", None) == 1:
                byte_loads += 1
            elif op in mlil.STORE_OPERATIONS and getattr(expr, "size", None) == 1:
                byte_stores += 1
            elif op == M.MLIL_XOR:
                has_xor = True
            elif op == M.MLIL_MUL:
                has_mul = True
            elif op == M.MLIL_AND:
                has_and = True
    return byte_loads >= 2 and byte_stores >= 1 and has_xor and has_mul and has_and


def _decode_driver_string_blob(bv, source_addr, spec):
    key_modulus = spec["key_modulus"]
    length = spec["length"]
    try:
        data = bv.read(source_addr, key_modulus + length)
    except Exception:  # noqa: BLE001
        return None
    if data is None or len(data) < key_modulus + length:
        return None

    out = bytearray()
    previous = 0
    for index in range(length):
        key_index = index % key_modulus
        key = data[key_index]
        cipher = data[key_modulus + index]
        if ((key_index * key) & 1) == 0:
            decoded = ((previous + cipher) & 0xFF) ^ ((~key) & 0xFF)
        else:
            decoded = (-(((cipher - previous) & 0xFF) ^ key)) & 0xFF
        plain = decoded ^ key
        out.append(plain)
        previous = plain
    return bytes(out)


def _last(il, bb):
    return il[bb.end - 1]


def _comparison_var(cond):
    operation = mlil.operation(cond)
    if operation not in _COMPARISON_OPERATIONS:
        return None
    variables = [
        var
        for var in (
            mlil.direct_var_from_expr(getattr(cond, "left", None)),
            mlil.direct_var_from_expr(getattr(cond, "right", None)),
        )
        if var is not None
    ]
    return variables[0] if len(variables) == 1 else None


def _comparison_details(il, if_il):
    condition = getattr(if_il, "condition", None)
    definition = None
    if mlil.operation(condition) == M.MLIL_VAR:
        try:
            ssa_definition = condition.function.ssa_form.get_ssa_var_definition(
                condition.ssa_form.src
            )
            definition = mlil.current_non_ssa_instruction(il, ssa_definition)
        except (AttributeError, KeyError, IndexError, TypeError):
            return None
        definition_block = getattr(definition, "il_basic_block", None)
        if (
            definition is None
            or mlil.operation(definition) != M.MLIL_SET_VAR
            or not mlil.same_var(getattr(definition, "dest", None), condition.src)
            or definition_block is None
            or definition_block.start != if_il.il_basic_block.start
            or definition.instr_index >= if_il.instr_index
        ):
            return None
        condition = getattr(definition, "src", None)
    return {
        "definition": definition,
        "parts": mlil.comparison_parts(condition),
        "use": definition or if_il,
        "var": _comparison_var(condition),
    }


def _router_prefix_is_pure(bb, details, root, state_vars):
    definition = details["definition"]
    definition_index = (
        getattr(definition, "instr_index", None)
        if definition is not None
        else None
    )
    saw_definition = definition is None
    for ins in list(bb)[:-1]:
        if definition_index is not None and ins.instr_index == definition_index:
            saw_definition = True
            continue
        if not _pure_router_instruction(ins, root, state_vars):
            return False
    return saw_definition


def _dispatcher_rows(il):
    rows = []
    for bb in il.basic_blocks:
        if_il = _last(il, bb)
        if mlil.operation(if_il) != M.MLIL_IF:
            continue
        details = _comparison_details(il, if_il)
        if details is None or details["parts"] is None:
            continue
        parts = details["parts"]
        chain = mlil.row_local_copy_chain(
            il,
            parts["var"],
            bb,
            details["use"],
        )
        if chain is None:
            continue
        state_vars = set(chain)
        if details["definition"] is not None:
            state_vars.add(details["definition"].dest)
        rows.append({
            "bb": bb,
            "if_il": if_il,
            "root": chain[-1],
            "state_vars": state_vars,
            "comparison": parts,
        })
    return rows


def _dominant_dispatcher_rows(il):
    groups = {}
    for row in _dispatcher_rows(il):
        groups.setdefault(
            (row["root"], row["comparison"]["bound"][1]),
            [],
        ).append(row)
    candidates = [group for group in groups.values() if len(group) >= _DISPATCHER_MIN_ROWS]
    if len(candidates) != 1:
        if candidates:
            log_warn("[driver_2_6:deflat] ambiguous dispatcher state roots; skipping")
        return None
    return candidates[0]


def _definition_dominates_use(il, definition, use):
    definition_block = getattr(definition, "il_basic_block", None)
    use_block = getattr(use, "il_basic_block", None)
    if definition_block is None or use_block is None:
        return False
    if definition_block.start == use_block.start:
        definition_index = getattr(definition, "instr_index", None)
        use_index = getattr(use, "instr_index", None)
        return (
            definition_index is not None
            and use_index is not None
            and definition_index < use_index
        )

    return definition_block in use_block.dominators


def _known_size(expr):
    size = getattr(expr, "size", None)
    return size if type(size) is int and size > 0 else None


def _write_matches_token_size(instruction, token_size):
    sizes = {
        size
        for size in (
            _known_size(instruction),
            _known_size(getattr(instruction, "src", None)),
        )
        if size is not None
    }
    return not sizes or sizes == {token_size}


def _expr_points_to_state(
    il,
    expr,
    root,
    use=None,
    seen=None,
    expected_size=None,
):
    expression_size = _known_size(expr)
    if (
        expected_size is not None
        and expression_size is not None
        and expression_size != expected_size
    ):
        return False
    operation = mlil.operation(expr)
    if operation == M.MLIL_ADDRESS_OF:
        return mlil.same_var(getattr(expr, "src", None), root)
    if operation in {M.MLIL_ADD, M.MLIL_SUB}:
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        right_is_zero = (
            mlil.operation(right) == M.MLIL_CONST
            and getattr(right, "constant", None) == 0
        )
        left_is_zero = (
            mlil.operation(left) == M.MLIL_CONST
            and getattr(left, "constant", None) == 0
        )
        pointer = None
        if right_is_zero:
            pointer = left
        elif operation == M.MLIL_ADD and left_is_zero:
            pointer = right
        if pointer is None:
            return False
        pointer_size = _known_size(pointer)
        if (
            expression_size is not None
            and pointer_size is not None
            and expression_size != pointer_size
        ):
            return False
        return _expr_points_to_state(
            il,
            pointer,
            root,
            use=use,
            seen=seen,
            expected_size=expression_size or expected_size,
        )
    var = mlil.direct_var_from_expr(expr)
    if var is None:
        return False
    seen = set(seen or ())
    if var in seen:
        return False
    seen.add(var)
    try:
        definitions = list(il.get_var_definitions(var))
    except Exception:  # noqa: BLE001
        return False
    if len(definitions) != 1:
        return False
    definition = definitions[0]
    if (
        mlil.operation(definition) != M.MLIL_SET_VAR
        or not mlil.same_var(getattr(definition, "dest", None), var)
        or (use is not None and not _definition_dominates_use(il, definition, use))
    ):
        return False
    definition_size = _known_size(definition)
    source = getattr(definition, "src", None)
    source_size = _known_size(source)
    known_sizes = {
        size
        for size in (expected_size, expression_size, definition_size, source_size)
        if size is not None
    }
    if len(known_sizes) > 1:
        return False
    return _expr_points_to_state(
        il,
        source,
        root,
        use=definition,
        seen=seen,
        expected_size=next(iter(known_sizes), None),
    )


def _pure_router_instruction(ins, root, state_vars):
    op = mlil.operation(ins)
    if op in {M.MLIL_GOTO, M.MLIL_NOP}:
        return True
    if op != M.MLIL_SET_VAR or mlil.same_var(getattr(ins, "dest", None), root):
        return False
    source = getattr(ins, "src", None)
    source_var = mlil.direct_var_from_expr(source)
    dest_size = getattr(ins, "size", None)
    source_size = getattr(source, "size", None)
    if dest_size is not None and source_size is not None and dest_size != source_size:
        return False
    return source_var is not None and all(
        any(mlil.same_var(var, candidate) for candidate in state_vars)
        for var in (ins.dest, source_var)
    )


def _router_boundary_block(il, bb, root, state_vars):
    tail = _last(il, bb)
    if mlil.operation(tail) == M.MLIL_IF:
        details = _comparison_details(il, tail)
        if (
            details is None
            or details["parts"] is None
            or not _router_prefix_is_pure(bb, details, root, state_vars)
        ):
            return False
        parts = details["parts"]
        chain = mlil.row_local_copy_chain(
            il,
            parts["var"],
            bb,
            details["use"],
        )
        return chain is not None and mlil.same_var(chain[-1], root)
    return all(_pure_router_instruction(ins, root, state_vars) for ins in bb)


def _expand_dispatcher_boundary(il, starts, root, state_vars):
    by_start = {bb.start: bb for bb in il.basic_blocks}
    expanded = set(starts)
    queue = deque(by_start[start] for start in starts if start in by_start)
    while queue:
        bb = queue.popleft()
        for edge in bb.incoming_edges:
            pred = edge.source
            if pred.start in expanded or not _router_boundary_block(
                il,
                pred,
                root,
                state_vars,
            ):
                continue
            expanded.add(pred.start)
            if len(pred.incoming_edges) <= 2:
                queue.append(pred)
    return expanded


def _analyze_driver_dispatcher(il):
    rows = _dominant_dispatcher_rows(il)
    if rows is None:
        return None
    root = rows[0]["root"]
    token_size = rows[0]["comparison"]["bound"][1]
    state_vars = set().union(*(row["state_vars"] for row in rows))
    if any(
        not _router_boundary_block(il, row["bb"], root, state_vars)
        for row in rows
    ):
        log_warn("[driver_2_6:deflat] dispatcher row contains an impure state update")
        return None
    dispatcher_starts = {row["bb"].start for row in rows}
    for bb in il.basic_blocks:
        tail = _last(il, bb)
        if mlil.operation(tail) != M.MLIL_IF:
            continue
        details = _comparison_details(il, tail)
        if details is None:
            continue
        parts = details["parts"]
        if parts is not None and parts["bound"][1] == token_size:
            comparison_var = parts["var"]
            chain = mlil.row_local_copy_chain(
                il,
                comparison_var,
                bb,
                details["use"],
            )
            if chain is None or not mlil.same_var(chain[-1], root):
                continue
            row_state_vars = set(chain)
            if details["definition"] is not None:
                row_state_vars.add(details["definition"].dest)
            candidate_state_vars = state_vars | row_state_vars
            if not _router_boundary_block(
                il,
                bb,
                root,
                candidate_state_vars,
            ):
                continue
            dispatcher_starts.add(bb.start)
            state_vars.update(row_state_vars)
    dispatcher_starts = _expand_dispatcher_boundary(
        il,
        dispatcher_starts,
        root,
        state_vars,
    )
    target_heads = {}
    for bb in il.basic_blocks:
        if bb.start not in dispatcher_starts:
            continue
        for edge in bb.outgoing_edges:
            if edge.target.start not in dispatcher_starts:
                target_heads.setdefault(edge.target.start, edge.target)
    return {
        "state_var": root,
        "state_vars": state_vars,
        "state_address_escapes": mlil.variable_address_escapes(il, root),
        "token_size": token_size,
        "dispatcher_starts": dispatcher_starts,
        "dispatcher_rows": {row["bb"].start: row for row in rows},
        "target_heads": tuple(target_heads.values()),
    }


def _tokens_from_expr(il, expr, token_size, scope, seen=None):
    seen = seen or set()
    if mlil.operation(expr) == M.MLIL_CONST:
        return {mlil.state_token(expr, token_size)}
    var = mlil.direct_var_from_expr(expr)
    if var is None:
        return None
    if var in seen:
        return None
    seen = set(seen)
    seen.add(var)

    tokens = set()
    found = False
    try:
        definitions = list(il.get_var_definitions(var))
    except Exception:  # noqa: BLE001
        return None
    for definition in definitions:
        definition_block = getattr(definition, "il_basic_block", None)
        if definition_block is None or definition_block.start not in scope:
            continue
        found = True
        resolved = _tokens_from_expr(il, definition.src, token_size, scope, set(seen))
        if not resolved:
            return None
        tokens.update(resolved)
    return tokens if found else None


def _store_targets_state(il, store, root):
    return _expr_points_to_state(il, store.dest, root, use=store)


def _store_may_target_state(il, store, root):
    return mlil.expression_may_address_variable(
        il,
        store.dest,
        root,
    )


def _state_channel_is_dispatcher_only(il, analysis, ignored_scope=()):
    root = analysis["state_var"]
    ignored_scope = set(ignored_scope)
    pointer_vars = set()
    for ins in getattr(il, "instructions", ()) or ():
        if mlil.operation(ins) != M.MLIL_SET_VAR:
            continue
        definitions = list(il.get_var_definitions(ins.dest))
        if len(definitions) == 1 and _expr_points_to_state(
            il,
            ins.src,
            root,
            use=ins,
        ):
            pointer_vars.add(ins.dest)

    def uses_channel(expr):
        if any(
            mlil.instruction_reads_variable(expr, variable)
            for variable in (*analysis["state_vars"], *pointer_vars)
        ):
            return True
        for node in mlil.walk_expr(expr):
            addressed = mlil.addressed_var(node)
            if addressed is not None and mlil.same_var(addressed, root):
                return True
        return False

    for bb in il.basic_blocks:
        if bb.start in analysis["dispatcher_starts"] or bb.start in ignored_scope:
            continue
        for ins in bb:
            op = mlil.operation(ins)
            if op != M.MLIL_SET_VAR and any(
                mlil.instruction_writes_variable(ins, state_var)
                for state_var in analysis["state_vars"]
            ):
                return False
            if (
                op == M.MLIL_SET_VAR
                and any(mlil.same_var(ins.dest, pointer) for pointer in pointer_vars)
                and _expr_points_to_state(il, ins.src, root, use=ins)
            ):
                continue
            if op == M.MLIL_STORE and _store_targets_state(il, ins, root):
                if uses_channel(ins.src):
                    return False
                continue
            if uses_channel(ins):
                return False
    return True


def _dispatcher_values_are_private(il, analysis):
    root = analysis["state_var"]
    dispatcher_values = {
        state_var
        for state_var in analysis["state_vars"]
        if not mlil.same_var(state_var, root)
    }
    return mlil.variables_are_scope_local(
        il,
        dispatcher_values,
        analysis["dispatcher_starts"],
    )


def _state_writes(il, analysis, scope):
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    writes = []
    for bb in il.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            op = mlil.operation(ins)
            if mlil.has_unmodeled_semantics(ins):
                return None
            if op == M.MLIL_SET_VAR and mlil.same_var(getattr(ins, "dest", None), root):
                if not _write_matches_token_size(ins, token_size):
                    return None
                resolved = _tokens_from_expr(il, ins.src, token_size, scope)
            elif mlil.instruction_writes_variable(ins, root):
                return None
            elif op in mlil.STORE_OPERATIONS:
                if op == M.MLIL_STORE and _store_targets_state(il, ins, root):
                    if not _write_matches_token_size(ins, token_size):
                        return None
                    resolved = _tokens_from_expr(il, ins.src, token_size, scope)
                elif _store_may_target_state(il, ins, root):
                    return None
                elif analysis["state_address_escapes"]:
                    return None
                else:
                    continue
            else:
                if (
                    mlil.has_unknown_memory_effect(ins)
                    and (
                        analysis["state_address_escapes"]
                        or mlil.expression_may_address_variable(il, ins, root)
                    )
                ):
                    return None
                continue
            if resolved is None or len(resolved) != 1:
                return None
            writes.append((ins, next(iter(resolved))))
    return writes


def _single_state_transition(il, analysis, scope, start):
    writes = _state_writes(il, analysis, scope)
    if not writes:
        return None
    tokens = {token for _ins, token in writes}
    if len(tokens) != 1:
        return None
    if not mlil.all_paths_hit_blocks(
        il.basic_blocks,
        {start.start},
        scope,
        {ins.il_basic_block.start for ins, _token in writes},
    ):
        return None
    dependency_exprs = []
    for ins, _token in writes:
        dependency_exprs.append(getattr(ins, "src", None))
        if mlil.operation(ins) == M.MLIL_STORE:
            dependency_exprs.append(getattr(ins, "dest", None))
    if not mlil.definitions_cover_all_paths(
        il,
        {start.start},
        scope,
        dependency_exprs,
    ):
        return None
    return next(iter(tokens)), writes


def _pure_state_selection_tail(il, analysis, scope, writes):
    root = analysis["state_var"]
    dependency_exprs = []
    write_indices = set()
    for ins, _token in writes:
        write_indices.add(ins.instr_index)
        dependency_exprs.append(getattr(ins, "src", None))
        if mlil.operation(ins) == M.MLIL_STORE:
            dependency_exprs.append(getattr(ins, "dest", None))
    required = mlil.dependency_variables(il, dependency_exprs, scope)
    written_dependencies = set()
    for bb in il.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            op = mlil.operation(ins)
            if op in {M.MLIL_IF, M.MLIL_GOTO, M.MLIL_NOP}:
                continue
            if op == M.MLIL_STORE and ins.instr_index in write_indices:
                continue
            if op != M.MLIL_SET_VAR:
                return False
            if mlil.same_var(ins.dest, root):
                continue
            if not any(mlil.same_var(ins.dest, var) for var in required):
                return False
            written_dependencies.add(ins.dest)
    return mlil.variables_are_scope_local(il, written_dependencies, scope)


def _private_exits(il, head, region, stop_starts):
    exits = {}
    for bb in il.basic_blocks:
        if bb.start not in region:
            continue
        foreign = [
            edge.source
            for edge in bb.incoming_edges
            if edge.source.start not in region
        ]
        if bb.start != head.start and foreign:
            return ()
        for edge in bb.outgoing_edges:
            if edge.target.start not in stop_starts:
                continue
            jump = _last(il, bb)
            if mlil.operation(jump) != M.MLIL_GOTO or len(bb.outgoing_edges) != 1:
                return ()
            exits[jump.instr_index] = (jump, edge.target)
    return tuple(exits.values())


def _route_dispatcher_token(il, analysis, start, token):
    current = start
    seen = set()
    while current.start in analysis["dispatcher_starts"]:
        if current.start in seen:
            return None
        seen.add(current.start)
        row = analysis["dispatcher_rows"].get(current.start)
        if row is not None:
            branch = mlil.evaluate_comparison(row["comparison"], token)
            if branch is None:
                return None
            current = il.get_basic_block_at(
                row["if_il"].true if branch else row["if_il"].false
            )
            continue
        if mlil.operation(_last(il, current)) == M.MLIL_IF:
            return None
        outgoing = list(current.outgoing_edges)
        if len(outgoing) != 1:
            return None
        current = outgoing[0].target
    return current


def _route_scope(il, analysis, scope, token):
    exits = {}
    for bb in il.basic_blocks:
        if bb.start not in scope:
            continue
        for edge in bb.outgoing_edges:
            if edge.target.start in analysis["dispatcher_starts"]:
                jump = list(bb)[-1]
                if mlil.operation(jump) != M.MLIL_GOTO or len(bb.outgoing_edges) != 1:
                    return None
                exits[jump.instr_index] = (jump, edge.target)
            elif edge.target.start not in scope:
                return None
    if not exits or not mlil.all_paths_reach_stops(
        il.basic_blocks,
        scope,
        analysis["dispatcher_starts"],
    ):
        return None
    targets = {}
    for _jump, start in exits.values():
        target = _route_dispatcher_token(il, analysis, start, token)
        if target is None:
            return None
        targets.setdefault(target.start, target)
    if len(targets) != 1:
        return None
    if not _dispatcher_values_are_private(il, analysis):
        return None
    return {
        "target": next(iter(targets.values())),
        "exits": tuple(exits.values()),
        "direct_rows": all(
            entry.start in analysis["dispatcher_rows"]
            for _jump, entry in exits.values()
        ),
    }


def _region_is_private(il, scope, owners):
    allowed = set(scope) | set(owners)
    return not any(
        bb.start not in owners
        and any(edge.source.start not in allowed for edge in bb.incoming_edges)
        for bb in il.basic_blocks
        if bb.start in scope
    )


def _owned_write_indices(il, writes, scope, owners):
    if not _region_is_private(il, scope, owners):
        return set()
    return {ins.instr_index for ins, _token in writes}


def _write_witnesses(writes, indices):
    indices = set(indices)
    return {
        ins.instr_index: ins
        for ins, _token in writes
        if ins.instr_index in indices
    }


def _entry_transition(il, analysis):
    if not il.basic_blocks:
        return None
    region = set()
    exits = []
    stop_starts = analysis["dispatcher_starts"]
    queue = deque([list(il.basic_blocks)[0]])
    while queue:
        bb = queue.popleft()
        if bb.start in region or bb.start in stop_starts:
            continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            if edge.target.start in stop_starts:
                exits.append((_last(il, bb), edge.target))
            else:
                queue.append(edge.target)
    entry = list(il.basic_blocks)[0]
    transition = _single_state_transition(il, analysis, region, entry)
    if transition is None or not exits:
        return None
    if not _region_is_private(il, region, {entry.start}):
        return None
    if not mlil.all_paths_reach_stops(il.basic_blocks, region, stop_starts):
        return None
    if any(
        mlil.operation(jump) != M.MLIL_GOTO
        or len(jump.il_basic_block.outgoing_edges) != 1
        for jump, _entry in exits
    ):
        return None
    entry_start = list(il.basic_blocks)[0].start
    if any(
        jump.il_basic_block.start != entry_start
        and any(
            edge.source.start not in region
            for edge in jump.il_basic_block.incoming_edges
        )
        for jump, _entry in exits
    ):
        return None
    token, _writes = transition
    targets = {}
    for _jump, dispatcher_entry in exits:
        target = _route_dispatcher_token(il, analysis, dispatcher_entry, token)
        if target is None:
            return None
        targets.setdefault(target.start, target)
    if len(targets) != 1:
        return None
    if not _dispatcher_values_are_private(il, analysis):
        return None
    return {
        "kind": "uncond",
        "exit_jumps": tuple(jump for jump, _entry in exits),
        "target_bb": next(iter(targets.values())),
        "obb": list(il.basic_blocks)[0],
        "state_token": token,
        "obsolete_state_writes": set(),
        "obsolete_state_write_witnesses": {},
        "entry": True,
    }


def _conditional_candidates(il, head, region, analysis):
    stop_starts = analysis["dispatcher_starts"]
    candidates = []
    for bb in il.basic_blocks:
        if bb.start not in region:
            continue
        if_il = _last(il, bb)
        if mlil.operation(if_il) != M.MLIL_IF:
            continue
        true_bb = il.get_basic_block_at(if_il.true)
        false_bb = il.get_basic_block_at(if_il.false)
        if true_bb.start in stop_starts or false_bb.start in stop_starts:
            continue
        true_scope = mlil.region_until(true_bb, stop_starts)
        false_scope = mlil.region_until(false_bb, stop_starts)
        true_transition = _single_state_transition(il, analysis, true_scope, true_bb)
        false_transition = _single_state_transition(il, analysis, false_scope, false_bb)
        if true_transition is None or false_transition is None:
            continue
        true_token, true_writes = true_transition
        false_token, false_writes = false_transition
        if true_token == false_token:
            continue
        writes = (*true_writes, *false_writes)
        scopes = true_scope | false_scope
        if not _pure_state_selection_tail(il, analysis, scopes, writes):
            continue
        true_route = _route_scope(il, analysis, true_scope, true_token)
        false_route = _route_scope(il, analysis, false_scope, false_token)
        if true_route is None or false_route is None:
            continue
        owned_writes = _owned_write_indices(il, writes, scopes, {head.start})
        exit_cleanup = (
            owned_writes
            if _state_channel_is_dispatcher_only(il, analysis)
            else set()
        )
        exit_targets = {}
        conflicting_exit = False
        for route in (true_route, false_route):
            for jump, _entry in route["exits"]:
                prior = exit_targets.get(jump.instr_index)
                if prior is not None and prior[1].start != route["target"].start:
                    conflicting_exit = True
                    break
                exit_targets[jump.instr_index] = (jump, route["target"])
            if conflicting_exit:
                break
        exact_writes = {ins.instr_index for ins, _token in writes}
        preserve_writes = (
            not conflicting_exit
            and true_route["direct_rows"]
            and false_route["direct_rows"]
            and owned_writes == exact_writes
            and _dispatcher_values_are_private(il, analysis)
        )
        bypass_safe = (
            owned_writes == exact_writes
            and _state_channel_is_dispatcher_only(il, analysis, scopes)
        )
        if not preserve_writes and not bypass_safe:
            continue
        cleanup_indices = exit_cleanup if preserve_writes else owned_writes
        candidates.append(
            {
                "kind": "if_else",
                "rewrite_mode": "arm_exits" if preserve_writes else "condition",
                "obb": head,
                "if_il": if_il,
                "true_target": true_route["target"],
                "false_target": false_route["target"],
                "true_token": true_token,
                "false_token": false_token,
                "exit_targets": (
                    tuple(exit_targets.values()) if preserve_writes else ()
                ),
                "obsolete_state_writes": cleanup_indices,
                "obsolete_state_write_witnesses": _write_witnesses(
                    writes,
                    cleanup_indices,
                ),
            }
        )
    return tuple(candidates)


def _head_transition(il, head, analysis):
    stop_starts = analysis["dispatcher_starts"]
    region = mlil.region_until(head, stop_starts)

    conditional = _conditional_candidates(il, head, region, analysis)
    if len(conditional) > 1:
        log_warn(f"[driver_2_6:deflat] {head.start}: ambiguous conditional transitions")
        return None
    if conditional:
        return conditional[0]

    transition = _single_state_transition(il, analysis, region, head)
    if transition is None:
        return None
    if not mlil.all_paths_reach_stops(il.basic_blocks, region, stop_starts):
        return None
    token, _writes = transition
    exits = _private_exits(il, head, region, stop_starts)
    if not exits:
        return None
    targets = {}
    for _jump, dispatcher_entry in exits:
        target = _route_dispatcher_token(il, analysis, dispatcher_entry, token)
        if target is None:
            return None
        targets.setdefault(target.start, target)
    if len(targets) != 1:
        return None
    if not _dispatcher_values_are_private(il, analysis):
        return None
    return {
        "kind": "uncond",
        "exit_jumps": tuple(jump for jump, _entry in exits),
        "target_bb": next(iter(targets.values())),
        "obb": head,
        "state_token": token,
        "obsolete_state_writes": set(),
        "obsolete_state_write_witnesses": {},
    }


def plan_deflatten_redirections(_bv, func, il):
    if il is None:
        return []
    analysis = _analyze_driver_dispatcher(il)
    if analysis is None:
        return []

    redirections = []
    entry = _entry_transition(il, analysis)
    if entry is not None:
        redirections.append(entry)

    heads = {}
    for head in analysis["target_heads"]:
        heads.setdefault(head.start, head)
    for head in heads.values():
        plan = _head_transition(il, head, analysis)
        if plan is not None:
            redirections.append(plan)

    if redirections:
        log_info(
            f"[driver_2_6:deflat] recovered {len(redirections)} transition(s) "
            f"in {hex(func.start)}"
        )
    return redirections
