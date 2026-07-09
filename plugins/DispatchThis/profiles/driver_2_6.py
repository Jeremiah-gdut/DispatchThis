from collections import deque

from . import valorant_2_6
from ..helpers import facts, memory, mlil
from ..utils.log import log_debug, log_info, log_warn


PROFILE_ID = "driver_2_6"
PROFILE_NAME = "Driver 2.6"
PROFILE_DESCRIPTION = (
    "Rules for the 2.6 driver binary: Valorant-compatible branch/call/global "
    "hooks plus driver deflattening and string decrypt."
)

# Supported:
# - branch gadget: valorant_2_6 delegate
# - indirect call gadget: valorant_2_6 delegate
# - global constants: valorant_2_6 delegate
# - deflatten: driver stack-state store dispatcher
# - string decrypt: driver clone decoder
#
# Validation:
# - deflatten: main @ 0x36d10, state var_124, state pointer var_168,
#   40 redirection plans recovered in driver.bndb.
# - string decrypt: main @ 0x36d10, 65 decrypt facts recovered in driver.bndb.

_DISPATCHER_MIN_ROWS = 3
_CONST_DATA_SECTIONS = {".data"}
_CONST_PTR_TYPE = valorant_2_6.CONST_SLOT_TYPE
_DRIVER_GLOBAL_CONSTANT_SOURCE_TYPES = {"void*", "int64_t"}
_U48 = 0xFFFFFFFFFFFF
_DIRECT_CALL_OPS = ("MLIL_CALL", "MLIL_CALL_UNTYPED", "MLIL_TAILCALL")


def resolve_branch_gadget(bv, il, known_targets=None):
    return valorant_2_6.resolve_branch_gadget(bv, il, known_targets)


def resolve_call_gadget(bv, il):
    return valorant_2_6.resolve_call_gadget(bv, il)


def plan_global_constant_slots(bv, il):
    plans = {
        plan["slot_addr"]: plan
        for plan in valorant_2_6.plan_global_constant_slots(bv, il)
    }
    for slot_addr, use_addr in _driver_global_constant_slot_refs(il):
        _add_driver_global_constant_plan(plans, bv, slot_addr, use_addr)
    return [plans[addr] for addr in sorted(plans)]


def _driver_global_constant_slot_refs(il):
    if il is None:
        return []
    refs = {}
    for call in mlil.iter_calls(il, _DIRECT_CALL_OPS):
        params = list(getattr(call, "params", ()) or ())
        if len(params) < 2:
            continue
        use_addr = getattr(call, "address", 0)
        for expr in mlil.walk_expr_with_defs(il, params[1], max_depth=32):
            for slot_addr, _offset in mlil.load_slot_offsets(il, expr, address_mask=_U48):
                refs.setdefault(slot_addr, use_addr)
    return refs.items()


def _add_driver_global_constant_plan(plans, bv, slot_addr, use_addr):
    if slot_addr in plans:
        return
    data_var = bv.get_data_var_at(slot_addr)
    type_name = str(getattr(data_var, "type", "")).replace(" ", "") if data_var is not None else ""
    if type_name not in _DRIVER_GLOBAL_CONSTANT_SOURCE_TYPES:
        return
    if not memory.in_section(bv, slot_addr, _CONST_DATA_SECTIONS):
        return
    value = memory.read_qword_slot(bv, slot_addr)
    if value is None:
        return
    plans[slot_addr] = facts.global_constant_fact(
        slot_addr,
        _CONST_PTR_TYPE,
        value,
        value & _U48,
        use_addr,
    )


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
        if mlil.op_name(side) == "MLIL_CONST":
            return side.constant
    return None


def _divu_constants(il):
    values = set()
    for ins in getattr(il, "instructions", ()) or ():
        for expr in mlil.walk_expr(ins):
            if mlil.op_name(expr) != "MLIL_DIVU":
                continue
            value = _const_from_binary_expr(expr)
            if value is not None:
                values.add(value)
    return values


def _var_defined_as_increment(il, var):
    for definition in il.get_var_definitions(var):
        src = getattr(definition, "src", None)
        if mlil.op_name(src) != "MLIL_ADD":
            continue
        if _const_from_binary_expr(src) == 1:
            return True
    return False


def _length_constants(il):
    values = set()
    for ins in getattr(il, "instructions", ()) or ():
        if mlil.op_name(ins) != "MLIL_IF":
            continue
        cond = getattr(ins, "condition", None)
        if mlil.op_name(cond) != "MLIL_CMP_E":
            continue
        sides = (getattr(cond, "left", None), getattr(cond, "right", None))
        const_expr = next((side for side in sides if mlil.op_name(side) == "MLIL_CONST"), None)
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
            op = mlil.op_name(expr)
            if op in mlil.LOAD_OPS and getattr(expr, "size", None) == 1:
                byte_loads += 1
            elif op in mlil.STORE_OPS and getattr(expr, "size", None) == 1:
                byte_stores += 1
            elif op == "MLIL_XOR":
                has_xor = True
            elif op == "MLIL_MUL":
                has_mul = True
            elif op == "MLIL_AND":
                has_and = True
    return byte_loads >= 2 and byte_stores >= 1 and has_xor and has_mul and has_and


def _decode_driver_string_blob(bv, source_addr, spec):
    key_modulus = spec["key_modulus"]
    length = spec["length"]
    data = bv.read(source_addr, key_modulus + length)
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


def _block_at(il, instr_index):
    return il[instr_index].il_basic_block


def _resolve_condition(cond):
    if mlil.op_name(cond) != "MLIL_VAR":
        return cond
    try:
        definition = cond.function.ssa_form.get_ssa_var_definition(cond.ssa_form.src)
    except AttributeError:
        return cond
    return getattr(definition, "src", cond) if definition is not None else cond


def _comparison_parts(cond):
    cond = _resolve_condition(cond)
    op = mlil.op_name(cond)
    if op not in ("MLIL_CMP_E", "MLIL_CMP_NE"):
        return None
    sides = (getattr(cond, "left", None), getattr(cond, "right", None))
    var_expr = next((side for side in sides if mlil.var_from_expr(side) is not None), None)
    const_expr = next((side for side in sides if mlil.op_name(side) == "MLIL_CONST"), None)
    if var_expr is None or const_expr is None:
        return None
    return op, mlil.var_from_expr(var_expr), mlil.state_token(const_expr)


def _trace_var_roots(il, var, seen=None, depth=0):
    seen = seen or set()
    key = str(var)
    if key in seen or depth > 12:
        return set()
    seen.add(key)

    definitions = list(il.get_var_definitions(var))
    if not definitions:
        return {var}

    roots = set()
    for definition in definitions:
        source_var = mlil.var_from_expr(getattr(definition, "src", None))
        if source_var is None:
            return {var}
        roots.update(_trace_var_roots(il, source_var, seen, depth + 1))
    return roots


def _dispatcher_rows(il):
    rows = []
    for bb in il.basic_blocks:
        if_il = _last(il, bb)
        if mlil.op_name(if_il) != "MLIL_IF":
            continue
        parts = _comparison_parts(if_il.condition)
        if parts is None:
            continue
        cmp_op, var, token = parts
        roots = _trace_var_roots(il, var)
        if len(roots) != 1:
            continue
        rows.append({
            "bb": bb,
            "if_il": if_il,
            "root": next(iter(roots)),
            "token": token,
            "target": if_il.false if cmp_op == "MLIL_CMP_NE" else if_il.true,
        })
    return rows


def _dominant_dispatcher_rows(il):
    groups = {}
    for row in _dispatcher_rows(il):
        groups.setdefault((str(row["root"]), row["token"][1]), []).append(row)
    candidates = [group for group in groups.values() if len(group) >= _DISPATCHER_MIN_ROWS]
    if not candidates:
        return None
    candidates.sort(key=len, reverse=True)
    if len(candidates) > 1 and len(candidates[1]) * 2 >= len(candidates[0]):
        log_warn("[driver_2_6:deflat] ambiguous dispatcher state roots; skipping")
        return None
    return candidates[0]


def _state_pointer_vars(il, root):
    pointers = set()
    for ins in getattr(il, "instructions", ()) or ():
        if mlil.op_name(ins) != "MLIL_SET_VAR":
            continue
        src = getattr(ins, "src", None)
        if mlil.op_name(src) == "MLIL_ADDRESS_OF" and mlil.same_var(getattr(src, "src", None), root):
            pointers.add(ins.dest)
    return pointers


def _router_boundary_block(il, bb, root):
    if any(
        mlil.op_name(ins) == "MLIL_SET_VAR" and mlil.same_var(getattr(ins, "dest", None), root)
        for ins in bb
    ):
        return False
    tail = _last(il, bb)
    if mlil.op_name(tail) == "MLIL_IF":
        parts = _comparison_parts(tail.condition)
        if parts is None:
            return False
        return any(mlil.same_var(candidate, root) for candidate in _trace_var_roots(il, parts[1]))
    return all(mlil.op_name(ins) in {"MLIL_SET_VAR", "MLIL_GOTO", "MLIL_NOP"} for ins in bb)


def _expand_dispatcher_boundary(il, starts, root):
    by_start = {bb.start: bb for bb in il.basic_blocks}
    expanded = set(starts)
    queue = deque(by_start[start] for start in starts if start in by_start)
    while queue:
        bb = queue.popleft()
        for edge in bb.incoming_edges:
            pred = edge.source
            if pred.start in expanded or not _router_boundary_block(il, pred, root):
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
    token_size = rows[0]["token"][1]
    token_targets = {row["token"]: _block_at(il, row["target"]) for row in rows}
    dispatcher_starts = _expand_dispatcher_boundary(il, {row["bb"].start for row in rows}, root)
    state_pointers = _state_pointer_vars(il, root)
    if not state_pointers:
        log_debug("[driver_2_6:deflat] dispatcher state root has no address-taken pointer")
        return None
    return {
        "state_var": root,
        "token_size": token_size,
        "token_targets": token_targets,
        "state_tokens": set(token_targets),
        "dispatcher_starts": dispatcher_starts,
        "state_pointers": state_pointers,
    }


def _region_until(il, start_bb, stop_starts, root, state_tokens):
    region = set()
    queue = deque([start_bb])
    while queue:
        bb = queue.popleft()
        if bb.start in region or bb.start in stop_starts:
            continue
        if bb.start != start_bb.start and mlil.op_name(_last(il, bb)) == "MLIL_IF":
            parts = _comparison_parts(_last(il, bb).condition)
            compares_state = parts is not None and (
                parts[2] in state_tokens
                or any(mlil.same_var(candidate, root) for candidate in _trace_var_roots(il, parts[1]))
            )
            if compares_state:
                continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            if edge.target.start not in region and edge.target.start not in stop_starts:
                queue.append(edge.target)
    return region


def _tokens_from_expr(il, expr, token_size, scope, seen=None):
    seen = seen or set()
    if mlil.op_name(expr) == "MLIL_CONST":
        return {mlil.state_token(expr, token_size)}
    var = mlil.var_from_expr(expr)
    if var is None:
        return set()
    key = str(var)
    if key in seen:
        return set()
    seen.add(key)

    tokens = set()
    for definition in il.get_var_definitions(var):
        if definition.il_basic_block.start not in scope:
            continue
        tokens.update(_tokens_from_expr(il, definition.src, token_size, scope, set(seen)))
    return tokens


def _store_targets_state_pointer(il, dest, state_pointers):
    var = mlil.var_from_expr(dest)
    if var is None:
        return False
    roots = _trace_var_roots(il, var)
    return any(
        mlil.same_var(root, pointer)
        for root in roots
        for pointer in state_pointers
    )


def _state_write_tokens(il, analysis, scope):
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    state_pointers = analysis["state_pointers"]
    tokens = set()
    for bb in il.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            op = mlil.op_name(ins)
            if op == "MLIL_SET_VAR" and mlil.same_var(getattr(ins, "dest", None), root):
                tokens.update(_tokens_from_expr(il, ins.src, token_size, scope))
            elif op == "MLIL_STORE" and _store_targets_state_pointer(il, ins.dest, state_pointers):
                tokens.update(_tokens_from_expr(il, ins.src, token_size, scope))
    return tokens


def _pure_state_selection_tail(il, analysis, scope):
    allowed = {"MLIL_SET_VAR", "MLIL_IF", "MLIL_GOTO", "MLIL_NOP"}
    for bb in il.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            op = mlil.op_name(ins)
            if op in allowed:
                continue
            if op == "MLIL_STORE" and _store_targets_state_pointer(
                il, ins.dest, analysis["state_pointers"]
            ):
                continue
            return False
    return True


def _private_exit(il, head, region, stop_starts):
    queue = deque([head])
    seen = set()
    while queue:
        bb = queue.popleft()
        if bb.start in seen:
            continue
        seen.add(bb.start)
        for edge in bb.outgoing_edges:
            succ = edge.target
            if succ.start in stop_starts:
                return _last(il, bb)
            if succ.start in region:
                foreign = [
                    incoming.source
                    for incoming in succ.incoming_edges
                    if incoming.source.start not in region and incoming.source.start not in stop_starts
                ]
                if foreign:
                    return _last(il, bb)
                queue.append(succ)
    return None


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
                exits.append(_last(il, bb))
            else:
                queue.append(edge.target)
    tokens = _state_write_tokens(il, analysis, region)
    if len(tokens) != 1 or len(exits) != 1:
        return None
    token = next(iter(tokens))
    target = analysis["token_targets"].get(token)
    if target is None:
        return None
    return {
        "kind": "uncond",
        "jump": exits[0],
        "target_bb": target,
        "obb": list(il.basic_blocks)[0],
        "state_var": analysis["state_var"],
        "state_vars": {analysis["state_var"]},
        "state_token": token,
        "state_tokens": {token},
        "entry": True,
    }


def _conditional_transition(il, head, region, analysis):
    stop_starts = analysis["dispatcher_starts"]
    root = analysis["state_var"]
    state_tokens = analysis["state_tokens"]
    token_targets = analysis["token_targets"]
    for bb in il.basic_blocks:
        if bb.start not in region:
            continue
        if_il = _last(il, bb)
        if mlil.op_name(if_il) != "MLIL_IF":
            continue
        true_bb = _block_at(il, if_il.true)
        false_bb = _block_at(il, if_il.false)
        if true_bb.start in stop_starts or false_bb.start in stop_starts:
            continue
        true_scope = _region_until(il, true_bb, stop_starts, root, state_tokens)
        false_scope = _region_until(il, false_bb, stop_starts, root, state_tokens)
        if not _pure_state_selection_tail(il, analysis, true_scope | false_scope):
            continue
        true_tokens = _state_write_tokens(il, analysis, true_scope)
        false_tokens = _state_write_tokens(il, analysis, false_scope)
        if len(true_tokens) != 1 or len(false_tokens) != 1:
            continue
        true_token = next(iter(true_tokens))
        false_token = next(iter(false_tokens))
        if true_token == false_token:
            continue
        true_target = token_targets.get(true_token)
        false_target = token_targets.get(false_token)
        if true_target is None or false_target is None:
            continue
        return {
            "kind": "if_else",
            "obb": head,
            "if_il": if_il,
            "true_target": true_target,
            "false_target": false_target,
            "true_token": true_token,
            "false_token": false_token,
            "state_var": root,
            "state_vars": {root},
            "state_tokens": {true_token, false_token},
        }
    return None


def _head_transition(il, head, analysis):
    stop_starts = analysis["dispatcher_starts"]
    region = _region_until(il, head, stop_starts, analysis["state_var"], analysis["state_tokens"])

    conditional = _conditional_transition(il, head, region, analysis)
    if conditional is not None:
        return conditional

    tokens = _state_write_tokens(il, analysis, region)
    if len(tokens) != 1:
        return None
    token = next(iter(tokens))
    target = analysis["token_targets"].get(token)
    if target is None:
        return None
    exit_jump = _private_exit(il, head, region, stop_starts)
    if exit_jump is None:
        return None
    return {
        "kind": "uncond",
        "jump": exit_jump,
        "target_bb": target,
        "obb": head,
        "state_var": analysis["state_var"],
        "state_vars": {analysis["state_var"]},
        "state_token": token,
        "state_tokens": {token},
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
    for head in analysis["token_targets"].values():
        heads.setdefault(head.start, head)
    for head in heads.values():
        plan = _head_transition(il, head, analysis)
        if plan is not None:
            redirections.append(plan)

    if redirections:
        log_info(
            f"[driver_2_6:deflat] recovered {len(redirections)} transition(s) "
            f"from {len(analysis['state_tokens'])} state token(s) in {hex(func.start)}"
        )
    return redirections
