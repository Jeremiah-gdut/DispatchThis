"""Deflatten cleanup: erase state writes after direct CFG edges are installed."""

from binaryninja import ILSourceLocation

from ...utils.log import log_info


_CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
_SET_VAR_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")
_STORE_OPS = ("MLIL_STORE", "MLIL_STORE_STRUCT")
_U32 = 0xFFFFFFFF


def _txt(instr):
    return str(instr).strip()


def _walk(expr):
    return list(expr.traverse(lambda x: x))


def _ref_consts(instr):
    values = set()
    for expr in _walk(instr):
        if expr.operation.name not in _CONST_OPS:
            continue
        values.add(expr.constant)
        values.add(expr.constant & _U32)
    return values


def _same_var(left, right):
    return left == right or str(left) == str(right)


def _has_var(var, vars_):
    return any(_same_var(var, candidate) for candidate in vars_)


def nop_state_writes(mlil, state_consts, state_vars):
    """NOP writes that only exist to feed the flattened dispatcher."""
    state_consts = state_consts or set()
    state_vars = state_vars or set()
    if not state_consts and not state_vars:
        log_info("[cleanup] state-writes: nothing recorded by deflatten; skipping")
        return 0

    seen = set()
    for ins in mlil.instructions:
        op = ins.operation.name
        if op not in _SET_VAR_OPS + _STORE_OPS or ins.instr_index in seen:
            continue

        by_var = op in _SET_VAR_OPS and _has_var(ins.dest, state_vars)
        by_value = bool(_ref_consts(ins.src) & state_consts)
        if not (by_var or by_value):
            continue

        seen.add(ins.instr_index)
        reason = []
        if by_var:
            reason.append(f"dest={ins.dest}")
        if by_value:
            reason.append("state-token")
        log_info(f"[cleanup] state-write NOP @ {hex(ins.address)} ({', '.join(reason)}): {_txt(ins)}")
        mlil.replace_expr(ins.expr_index, mlil.nop(ILSourceLocation.from_instruction(ins)))

    if seen:
        mlil.finalize()
        mlil.generate_ssa_form()
    log_info(
        f"[cleanup] state-writes: NOP'd {len(seen)} write(s) "
        f"({len(state_consts)} state const(s), {len(state_vars)} state var/alias(es))"
    )
    return len(seen)


def clean_resolved_gadget_jumps(bv, func, mlil=None):
    """Workflow cleanup hook kept for compatibility; deflatten now only owns state writes."""
    mlil = mlil or func.medium_level_il
    if mlil is None:
        return 0, 0, 0, 0

    state_consts = bv.session_data.get("dispatchthis_state_consts", {}).get(func.start, set())
    state_vars = bv.session_data.get("dispatchthis_state_vars", {}).get(func.start, set())
    nopd_state_writes = nop_state_writes(mlil, state_consts, state_vars)
    return 0, 0, 0, nopd_state_writes
