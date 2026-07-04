"""Recover if/else branches hidden behind resolved indirect-branch switches."""

from binaryninja import ILSourceLocation, MediumLevelILLabel

from .phase_cleanup import mlil_def_roots
from ...utils.log import log_info, log_warn


U48 = 0xFFFFFFFFFFFF
U64 = 0xFFFFFFFFFFFFFFFF
_CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")


def _label(idx):
    label = MediumLevelILLabel()
    label.operand = idx
    return label


def _const(expr):
    return expr.constant & U64 if expr.operation.name in _CONST_OPS else None


def _single_var_def(mlil, var):
    defs = mlil.get_var_definitions(var)
    return defs[0] if len(defs) == 1 else None


def _eval_const(bv, mlil, expr, overrides, depth=0):
    if expr is None or depth > 64:
        return None
    op = expr.operation.name

    if op in _CONST_OPS:
        return expr.constant & U64

    if op == "MLIL_VAR":
        if expr.src in overrides:
            return overrides[expr.src] & U64
        d = _single_var_def(mlil, expr.src)
        return None if d is None else _eval_const(bv, mlil, d.src, overrides, depth + 1)

    if op in ("MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"):
        return _eval_const(bv, mlil, expr.src, overrides, depth + 1)

    if op in ("MLIL_ADD", "MLIL_SUB", "MLIL_AND", "MLIL_OR", "MLIL_XOR", "MLIL_LSL", "MLIL_LSR"):
        l = _eval_const(bv, mlil, expr.left, overrides, depth + 1)
        r = _eval_const(bv, mlil, expr.right, overrides, depth + 1)
        if l is None or r is None:
            return None
        if op == "MLIL_ADD":
            return (l + r) & U64
        if op == "MLIL_SUB":
            return (l - r) & U64
        if op == "MLIL_AND":
            return (l & r) & U64
        if op == "MLIL_OR":
            return (l | r) & U64
        if op == "MLIL_XOR":
            return (l ^ r) & U64
        if op == "MLIL_LSL":
            return (l << r) & U64
        return (l >> r) & U64

    if op in _LOAD_OPS:
        addr = _eval_const(bv, mlil, expr.src, overrides, depth + 1)
        if addr is None:
            return None
        data = bv.read(addr & U48, expr.size)
        if len(data) != expr.size:
            return None
        return int.from_bytes(data, "little")

    try:
        value = expr.value
    except Exception:  # noqa: BLE001
        return None
    if value.type.name in ("ConstantValue", "ConstantPointerValue", "ImportedAddressValue"):
        return value.value & U64
    return None


def _target_idx_for_value(bv, mlil, jump_il, var, value):
    target = _eval_const(bv, mlil, jump_il.dest, {var: value})
    if target is None:
        return None
    target &= U48
    for addr, idx in jump_il.targets.items():
        if (addr & U48) == target:
            return idx
    return None


def _const_assigns(mlil, bb):
    assigns = {}
    for i in range(bb.start, bb.end):
        ins = mlil[i]
        if ins.operation.name != "MLIL_SET_VAR":
            continue
        value = _const(ins.src)
        if value is not None:
            assigns[ins.dest] = (value, ins)
    return assigns


def _source_if_for_arm(mlil, bb):
    for edge in bb.incoming_edges:
        pred = edge.source
        term = mlil[pred.end - 1]
        if term.operation.name != "MLIL_IF":
            continue
        if term.true == bb.start or term.false == bb.start:
            return term
    return None


def _condition_expr(mlil, if_il):
    cond = if_il.condition
    if cond.operation.name != "MLIL_VAR":
        return cond
    d = _single_var_def(mlil, cond.src)
    return d.src if d is not None else cond


def _plan_for_jump(bv, mlil, jump_il):
    if jump_il.operation.name != "MLIL_JUMP_TO" or len(jump_il.targets) != 2:
        return None

    join = jump_il.il_basic_block
    arms = [edge.source for edge in join.incoming_edges]
    if len(arms) != 2:
        return None

    true_if = _source_if_for_arm(mlil, arms[0])
    false_if = _source_if_for_arm(mlil, arms[1])
    if true_if is None or false_if is None or true_if.expr_index != false_if.expr_index:
        return None
    if_il = true_if

    true_arm = mlil[if_il.true].il_basic_block
    false_arm = mlil[if_il.false].il_basic_block
    if {true_arm.start, false_arm.start} != {bb.start for bb in arms}:
        return None

    true_assigns = _const_assigns(mlil, true_arm)
    false_assigns = _const_assigns(mlil, false_arm)
    for var in set(true_assigns) & set(false_assigns):
        true_value, true_assign = true_assigns[var]
        false_value, false_assign = false_assigns[var]
        true_idx = _target_idx_for_value(bv, mlil, jump_il, var, true_value)
        false_idx = _target_idx_for_value(bv, mlil, jump_il, var, false_value)
        if true_idx is None or false_idx is None or true_idx == false_idx:
            continue
        return {
            "condition": _condition_expr(mlil, if_il),
            "true": true_idx,
            "false": false_idx,
            "cleanup_roots": (
                mlil_def_roots(mlil, jump_il.dest)
                | {true_assign.instr_index, false_assign.instr_index}
            ),
        }

    return None


def translate_indirect_branch_conditions(bv, mlil):
    """Translate resolved two-target MLIL_JUMP_TO gadgets back to MLIL_IF in place."""
    plans = []
    for ins in list(mlil.instructions):
        plan = _plan_for_jump(bv, mlil, ins)
        if plan is not None:
            plans.append((ins, plan))

    cleanup_roots = set()
    for _, plan in plans:
        cleanup_roots.update(plan["cleanup_roots"])

    if not plans:
        return mlil, 0, cleanup_roots

    applied = 0
    for jump_il, plan in plans:
        try:
            mlil.replace_expr(
                jump_il.expr_index,
                mlil.if_expr(
                    mlil.copy_expr(plan["condition"]),
                    _label(plan["true"]),
                    _label(plan["false"]),
                    ILSourceLocation.from_instruction(jump_il),
                ),
            )
            applied += 1
        except Exception as e:  # noqa: BLE001
            log_warn(f"[branch-conditions] failed to translate {hex(jump_il.address)}: {e}")

    if applied:
        # ponytail: avoid assigning a fresh MLIL function; BN can keep regenerating
        # HLIL when workflow code swaps analysis_context.mlil during HLIL generation.
        mlil.finalize()
        mlil.generate_ssa_form()
        log_info(f"[branch-conditions] translated {applied} indirect branch switch(es) to if/else")
    return mlil, applied, cleanup_roots
