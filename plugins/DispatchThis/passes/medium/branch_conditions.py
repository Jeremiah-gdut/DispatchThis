"""Recover if/else branches hidden behind resolved indirect-branch switches."""

from binaryninja import (
    ILSourceLocation,
    MediumLevelILOperation as M,
    RegisterValueType,
)

from ...helpers.mlil import (
    address_escape_checker,
    cleanup_roots_for_expr,
    dependency_variables,
    instruction_writes_variable,
    same_var,
    set_roots_before_instruction,
    scope_locality_checker,
    variable_address_escapes,
    variables_are_scope_local,
    walk_expr,
)
from ...utils.log import log_info, log_warn
from .rewrite import copied_label_for_source, copy_mlil_with_instruction_rewrites


U48 = 0xFFFFFFFFFFFF
U64 = 0xFFFFFFFFFFFFFFFF
_CONST_OPS = {M.MLIL_CONST, M.MLIL_CONST_PTR}
_LOAD_OPS = {
    M.MLIL_LOAD,
    M.MLIL_LOAD_SSA,
    M.MLIL_LOAD_STRUCT,
    M.MLIL_LOAD_STRUCT_SSA,
}
_ARITHMETIC_OPS = {
    M.MLIL_ADD,
    M.MLIL_SUB,
    M.MLIL_MUL,
    M.MLIL_AND,
    M.MLIL_OR,
    M.MLIL_XOR,
    M.MLIL_LSL,
    M.MLIL_LSR,
}
_DECODE_VALUE_OPS = (
    _CONST_OPS
    | _LOAD_OPS
    | _ARITHMETIC_OPS
    | {
        M.MLIL_VAR,
        M.MLIL_ZX,
        M.MLIL_SX,
        M.MLIL_LOW_PART,
        M.MLIL_NEG,
    }
)
_CONSTANT_VALUE_TYPES = {
    RegisterValueType.ConstantValue,
    RegisterValueType.ConstantPointerValue,
    RegisterValueType.ImportedAddressValue,
}


def _const(expr):
    return expr.constant & _expr_mask(expr) if expr.operation in _CONST_OPS else None


def _expr_mask(expr):
    size = getattr(expr, "size", None) or 8
    return (1 << min(size * 8, 64)) - 1


def _cast_value(op, expr, value):
    result = value & _expr_mask(expr)
    if op != M.MLIL_SX:
        return result
    source = getattr(expr, "src", None)
    source_bits = min((getattr(source, "size", None) or 8) * 8, 64)
    source_mask = (1 << source_bits) - 1
    value &= source_mask
    sign = 1 << (source_bits - 1)
    signed = value - (1 << source_bits) if value & sign else value
    return signed & _expr_mask(expr)


def _eval_const(bv, mlil, expr, overrides, depth=0):
    if expr is None or depth > 64:
        return None
    op = expr.operation

    bool_expr, bool_value = overrides.get("__bool_to_int__", (None, None))
    expr_index = getattr(expr, "expr_index", None)
    bool_index = getattr(bool_expr, "expr_index", None)
    same_bool = expr_index == bool_index if expr_index is not None and bool_index is not None else expr is bool_expr
    if op == M.MLIL_BOOL_TO_INT and same_bool:
        return bool_value & U64

    if op in _CONST_OPS:
        return expr.constant & _expr_mask(expr)

    if op == M.MLIL_VAR:
        if expr.src in overrides:
            return overrides[expr.src] & _expr_mask(expr)
        values = set()
        for definition in mlil.get_var_definitions(expr.src):
            value = _eval_const(bv, mlil, definition.src, overrides, depth + 1)
            if value is None:
                return None
            values.add(value)
        return values.pop() if len(values) == 1 else None

    if op in {M.MLIL_ZX, M.MLIL_SX, M.MLIL_LOW_PART}:
        value = _eval_const(bv, mlil, expr.src, overrides, depth + 1)
        return None if value is None else _cast_value(op, expr, value)

    if op == M.MLIL_NEG:
        value = _eval_const(bv, mlil, expr.src, overrides, depth + 1)
        return None if value is None else (-value) & _expr_mask(expr)

    if op in _ARITHMETIC_OPS:
        left = _eval_const(bv, mlil, expr.left, overrides, depth + 1)
        right = _eval_const(bv, mlil, expr.right, overrides, depth + 1)
        if left is None or right is None:
            return None
        if op == M.MLIL_ADD:
            return (left + right) & _expr_mask(expr)
        if op == M.MLIL_SUB:
            return (left - right) & _expr_mask(expr)
        if op == M.MLIL_MUL:
            return (left * right) & _expr_mask(expr)
        if op == M.MLIL_AND:
            return (left & right) & _expr_mask(expr)
        if op == M.MLIL_OR:
            return (left | right) & _expr_mask(expr)
        if op == M.MLIL_XOR:
            return (left ^ right) & _expr_mask(expr)
        if op == M.MLIL_LSL:
            return (left << right) & _expr_mask(expr)
        return (left >> right) & _expr_mask(expr)

    if op in _LOAD_OPS:
        addr = _eval_const(bv, mlil, expr.src, overrides, depth + 1)
        if addr is None:
            return None
        if op in {M.MLIL_LOAD_STRUCT, M.MLIL_LOAD_STRUCT_SSA}:
            offset = getattr(expr, "offset", None)
            if type(offset) is not int:
                return None
            addr += offset
        # The validated DYZZNB target tables live in semantically writable
        # segments. The recovered value is accepted only when it maps back to
        # the current JUMP_TO target set, so segment writability is not a proof
        # boundary here.
        try:
            data = bv.read(addr & U48, expr.size)
        except Exception:  # noqa: BLE001
            return None
        if data is None:
            return None
        if len(data) != expr.size:
            return None
        return int.from_bytes(data, "little")

    try:
        value = expr.value
    except Exception:  # noqa: BLE001
        return None
    if value.type in _CONSTANT_VALUE_TYPES:
        return value.value & U64
    return None


def _direct_bool_to_int(expr):
    matches = [node for node in walk_expr(expr) if node.operation == M.MLIL_BOOL_TO_INT]
    return matches[0] if len(matches) == 1 else None


def _target_for_value(bv, mlil, jump_il, var, value):
    target = _eval_const(bv, mlil, jump_il.dest, {var: value})
    if target is None:
        return None
    return _unique_target_index(jump_il, target)


def _target_for_bool(bv, mlil, jump_il, bool_expr, value):
    target = _eval_const(bv, mlil, jump_il.dest, {"__bool_to_int__": (bool_expr, value)})
    if target is None:
        return None
    return _unique_target_index(jump_il, target)


def _unique_target_index(jump_il, target):
    matches = {
        idx
        for addr, idx in jump_il.targets.items()
        if (addr & U48) == (target & U48)
    }
    return matches.pop() if len(matches) == 1 else None


def _const_assigns(mlil, bb):
    assigns = {}
    for i in range(bb.start, bb.end):
        ins = mlil[i]
        for variable in tuple(assigns):
            if instruction_writes_variable(ins, variable):
                assigns.pop(variable)
        if ins.operation != M.MLIL_SET_VAR:
            continue
        value = _const(ins.src)
        if value is not None:
            assigns[ins.dest] = (value, ins)
    return assigns


def _assign_size(assign):
    return getattr(assign, "size", None) or getattr(getattr(assign, "src", None), "size", None) or 8


def _assigned_target_candidates(bv, mlil, jump_il, true_assigns, false_assigns):
    candidates = []
    for var, (true_value, true_assign) in true_assigns.items():
        false_entry = false_assigns.get(var)
        if false_entry is None:
            continue
        false_value, false_assign = false_entry
        if (
            true_value == false_value
            or _assign_size(true_assign) != _assign_size(false_assign)
        ):
            continue
        true_idx = _target_for_value(bv, mlil, jump_il, var, true_value)
        false_idx = _target_for_value(bv, mlil, jump_il, var, false_value)
        if true_idx is None or false_idx is None or true_idx == false_idx:
            continue
        candidates.append({
            "condition_var": var,
            "condition_size": _assign_size(true_assign),
            "condition_value": true_value,
            "true": true_idx,
            "false": false_idx,
            "true_assign": true_assign,
            "false_assign": false_assign,
        })
    return candidates


def _consensus_target_mapping(candidates):
    """Return one mapping only after every independently valid witness agrees."""
    mappings = {(candidate["true"], candidate["false"]) for candidate in candidates}
    return mappings.pop() if len(mappings) == 1 else None


def _plan_from_candidates(mlil, jump_il, candidates):
    if _consensus_target_mapping(candidates) is None:
        return None

    # All witnesses establish the same branch semantics. The first block-order
    # witness is therefore a condition representation, not a pruned target.
    representative = candidates[0]
    return {
        "condition_var": representative["condition_var"],
        "condition_size": representative["condition_size"],
        "condition_value": representative["condition_value"],
        "true": representative["true"],
        "false": representative["false"],
        "cleanup_roots": cleanup_roots_for_expr(mlil, jump_il.dest),
    }


def _plan_for_assigned_target_var(bv, mlil, jump_il, true_assigns, false_assigns):
    return _plan_from_candidates(
        mlil,
        jump_il,
        _assigned_target_candidates(bv, mlil, jump_il, true_assigns, false_assigns),
    )


def _source_if_for_arm(mlil, bb):
    incoming = tuple(bb.incoming_edges)
    if len(incoming) != 1:
        return None
    pred = incoming[0].source
    term = mlil[pred.end - 1]
    if term.operation != M.MLIL_IF:
        return None
    return term if term.true == bb.start or term.false == bb.start else None


def _condition_expr(mlil, if_il):
    cond = if_il.condition
    # The translated IF executes after both arms. Preserve the predicate value
    # captured at the source IF; re-evaluating an expression there could observe
    # arm writes or repeat a load. Direct expressions therefore fail closed.
    return cond if cond.operation == M.MLIL_VAR else None


def _bool_cond(bool_expr):
    # This replacement stays at the original JUMP_TO, so its existing operand
    # is the exact condition representation to copy.
    return bool_expr.src


def _arms_preserve_variable(arms, variable):
    return all(
        not instruction_writes_variable(instruction, variable)
        for arm in arms
        for instruction in arm
    )


def _join_prefix(jump_il):
    try:
        instructions = list(jump_il.il_basic_block)
    except (AttributeError, TypeError):
        return None
    jump_index = getattr(jump_il, "instr_index", None)
    if (
        not instructions
        or type(jump_index) is not int
        or getattr(instructions[-1], "instr_index", None) != jump_index
    ):
        return None
    return instructions[:-1]


def _owned_decode_source(
    mlil,
    jump_il,
    true_arm,
    false_arm,
    candidates,
    address_escapes=None,
    variables_are_local=None,
):
    """Return the existing source IF when one private decode diamond is proved."""
    join = jump_il.il_basic_block
    join_instructions = list(join)
    if address_escapes is None:
        def address_escapes(variable):
            return variable_address_escapes(mlil, variable)
    if variables_are_local is None:
        def variables_are_local(variables, scope):
            return variables_are_scope_local(mlil, variables, scope)
    if (
        not join_instructions
        or join_instructions[-1].instr_index != jump_il.instr_index
        or len(join.incoming_edges) != 2
        or {edge.source.start for edge in join.incoming_edges}
        != {true_arm.start, false_arm.start}
        or len(join.outgoing_edges) != 2
        or {edge.target.start for edge in join.outgoing_edges}
        != set(jump_il.targets.values())
    ):
        return None

    source_ifs = (
        _source_if_for_arm(mlil, true_arm),
        _source_if_for_arm(mlil, false_arm),
    )
    if (
        source_ifs[0] is None
        or source_ifs[1] is None
        or source_ifs[0].expr_index != source_ifs[1].expr_index
    ):
        return None
    source_if = source_ifs[0]
    source = source_if.il_basic_block
    ordered_arms = (
        mlil[source_if.true].il_basic_block,
        mlil[source_if.false].il_basic_block,
    )
    if (
        ordered_arms[0] is None
        or ordered_arms[1] is None
        or ordered_arms[0].start == ordered_arms[1].start
        or {arm.start for arm in ordered_arms} != {true_arm.start, false_arm.start}
        or len(source.outgoing_edges) != 2
        or {edge.target.start for edge in source.outgoing_edges}
        != {true_arm.start, false_arm.start}
    ):
        return None

    for arm in ordered_arms:
        instructions = list(arm)
        if (
            not instructions
            or len(arm.incoming_edges) != 1
            or arm.incoming_edges[0].source.start != source.start
            or len(arm.outgoing_edges) != 1
            or arm.outgoing_edges[0].target.start != join.start
            or instructions[-1].operation != M.MLIL_GOTO
        ):
            return None
        for instruction in instructions[:-1]:
            if instruction.operation == M.MLIL_NOP:
                continue
            source_expr = getattr(instruction, "src", None)
            if (
                instruction.operation != M.MLIL_SET_VAR
                or source_expr is None
                or _const(source_expr) is None
                or getattr(instruction, "size", None)
                != getattr(source_expr, "size", None)
            ):
                return None

    for instruction in join_instructions[:-1]:
        if instruction.operation == M.MLIL_NOP:
            continue
        source_expr = getattr(instruction, "src", None)
        if (
            instruction.operation != M.MLIL_SET_VAR
            or source_expr is None
            or getattr(instruction, "size", None)
            != getattr(source_expr, "size", None)
            or any(node.operation not in _DECODE_VALUE_OPS for node in walk_expr(source_expr))
        ):
            return None

    target_mapping = _consensus_target_mapping(candidates)
    if target_mapping is None:
        return None

    scope = {true_arm.start, false_arm.start, join.start}
    written = {
        instruction.dest
        for block in (*ordered_arms, join)
        for instruction in block
        if instruction.operation == M.MLIL_SET_VAR
    }
    dependencies = dependency_variables(mlil, (jump_il.dest,), scope)
    if (
        not written
        or any(
            not any(same_var(variable, dependency) for dependency in dependencies)
            for variable in written
        )
        or not variables_are_local(written, scope)
        or any(address_escapes(variable) for variable in written)
    ):
        return None
    return source_if, target_mapping


def _plan_for_jump(
    bv,
    mlil,
    jump_il,
    address_escapes=None,
    variables_are_local=None,
):
    if jump_il.operation != M.MLIL_JUMP_TO or len(jump_il.targets) != 2:
        return None

    bool_expr = _direct_bool_to_int(jump_il.dest)
    if bool_expr is not None:
        true_idx = _target_for_bool(bv, mlil, jump_il, bool_expr, 1)
        false_idx = _target_for_bool(bv, mlil, jump_il, bool_expr, 0)
        if true_idx is not None and false_idx is not None and true_idx != false_idx:
            return {
                "condition": _bool_cond(bool_expr),
                "true": true_idx,
                "false": false_idx,
                "cleanup_roots": cleanup_roots_for_expr(mlil, jump_il.dest),
            }

    join = jump_il.il_basic_block
    join_prefix = _join_prefix(jump_il)
    if join_prefix is None:
        return None
    arms = [edge.source for edge in join.incoming_edges]
    if len(arms) != 2:
        return None

    arm_assigns = [_const_assigns(mlil, arm) for arm in arms]
    assign_candidates = [
        candidate
        for candidate in _assigned_target_candidates(
            bv,
            mlil,
            jump_il,
            arm_assigns[0],
            arm_assigns[1],
        )
        if _arms_preserve_variable((join_prefix,), candidate["condition_var"])
    ]
    assign_plan = _plan_from_candidates(mlil, jump_il, assign_candidates)

    true_if = _source_if_for_arm(mlil, arms[0])
    false_if = _source_if_for_arm(mlil, arms[1])
    if true_if is None or false_if is None or true_if.expr_index != false_if.expr_index:
        return assign_plan
    if_il = true_if

    true_arm = mlil[if_il.true].il_basic_block
    false_arm = mlil[if_il.false].il_basic_block
    if {true_arm.start, false_arm.start} != {bb.start for bb in arms}:
        return assign_plan

    true_assigns = _const_assigns(mlil, true_arm)
    false_assigns = _const_assigns(mlil, false_arm)
    candidates = _assigned_target_candidates(
        bv,
        mlil,
        jump_il,
        true_assigns,
        false_assigns,
    )
    candidates = [
        candidate
        for candidate in candidates
        if _arms_preserve_variable((join_prefix,), candidate["condition_var"])
    ]
    owned_args = (mlil, jump_il, true_arm, false_arm, candidates)
    owned_source = (
        _owned_decode_source(*owned_args)
        if address_escapes is None and variables_are_local is None
        else _owned_decode_source(
            *owned_args,
            address_escapes,
            variables_are_local,
        )
    )
    if owned_source is not None:
        source_if, (true_idx, false_idx) = owned_source
        return {
            "rewrite_il": source_if,
            "condition_from_rewrite": True,
            "true": true_idx,
            "false": false_idx,
            "cleanup_roots": (
                cleanup_roots_for_expr(mlil, jump_il.dest)
                | {
                    assign.instr_index
                    for candidate in candidates
                    for assign in (candidate["true_assign"], candidate["false_assign"])
                }
                | set_roots_before_instruction(mlil, source_if)
            ),
        }

    condition = _condition_expr(mlil, if_il)
    condition_address_escapes = condition is not None and (
        variable_address_escapes(mlil, condition.src)
        if address_escapes is None
        else address_escapes(condition.src)
    )
    if (
        condition is None
        or condition_address_escapes
        or not _arms_preserve_variable(arms, condition.src)
        or not _arms_preserve_variable((join_prefix,), condition.src)
    ):
        return assign_plan

    source_prefix_roots = set_roots_before_instruction(mlil, if_il)
    target_mapping = _consensus_target_mapping(candidates)
    if target_mapping is not None:
        true_idx, false_idx = target_mapping
        return {
            "condition": condition,
            "true": true_idx,
            "false": false_idx,
            "cleanup_roots": (
                cleanup_roots_for_expr(mlil, jump_il.dest)
                | {
                    assign.instr_index
                    for candidate in candidates
                    for assign in (candidate["true_assign"], candidate["false_assign"])
                }
                | source_prefix_roots
            ),
        }

    if assign_plan is not None:
        assign_plan["cleanup_roots"] = (
            set(assign_plan["cleanup_roots"]) | source_prefix_roots
        )
    return assign_plan


def _condition_for_plan(mlil, plan, rewrite_il):
    if plan.get("condition_from_rewrite"):
        condition = rewrite_il.condition
        copy_to = getattr(condition, "copy_to", None)
        return copy_to(mlil) if copy_to is not None else mlil.copy_expr(condition)
    if "condition" in plan:
        condition = plan["condition"]
        copy_to = getattr(condition, "copy_to", None)
        if copy_to is not None:
            return copy_to(mlil)
        return mlil.copy_expr(condition)
    size = plan["condition_size"]
    return mlil.compare_equal(
        0,
        mlil.var(size, plan["condition_var"]),
        mlil.const(size, plan["condition_value"]),
    )


def _replacement_for_plan(plan):
    def replace(new_mlil, rewrite_il):
        return new_mlil.if_expr(
            _condition_for_plan(new_mlil, plan, rewrite_il),
            copied_label_for_source(new_mlil, plan["true"]),
            copied_label_for_source(new_mlil, plan["false"]),
            ILSourceLocation.from_instruction(rewrite_il),
        )

    return replace


def translate_indirect_branch_conditions(bv, ctx, mlil):
    """Translate resolved two-target MLIL_JUMP_TO gadgets back to MLIL_IF.

    A ``None`` replacement reports that selected translations were rejected.
    """
    address_escapes = address_escape_checker(mlil)
    variables_are_local = scope_locality_checker(mlil)

    plans = []
    for ins in list(mlil.instructions):
        plan = _plan_for_jump(
            bv,
            mlil,
            ins,
            address_escapes,
            variables_are_local,
        )
        if plan is not None:
            plans.append((ins, plan))

    cleanup_roots = set()
    for _, plan in plans:
        cleanup_roots.update(plan["cleanup_roots"])

    if not plans:
        return mlil, 0, cleanup_roots

    replacements = {}
    for jump_il, plan in plans:
        rewrite_il = plan.get("rewrite_il", jump_il)
        instr_index = getattr(rewrite_il, "instr_index", None)
        if instr_index is None:
            log_warn(
                f"[branch-conditions] rejected translation at {hex(jump_il.address)} "
                "without instruction index"
            )
            return None, 0, set()
        if instr_index in replacements:
            log_warn(
                f"[branch-conditions] rejected duplicate rewrite at instruction {instr_index}"
            )
            return None, 0, set()
        replacements[instr_index] = _replacement_for_plan(plan)
    if not replacements:
        return None, 0, set()

    try:
        new_mlil, applied = copy_mlil_with_instruction_rewrites(ctx, replacements, mlil=mlil)
    except Exception as e:  # noqa: BLE001
        log_warn(f"[branch-conditions] failed to translate branch conditions: {e}")
        return None, 0, set()

    if applied:
        log_info(f"[branch-conditions] translated {applied} indirect branch switch(es) to if/else")
        return new_mlil, applied, cleanup_roots
    return None, 0, set()
