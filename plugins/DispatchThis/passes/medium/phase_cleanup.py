"""One-shot phase cleanup for resolved target decode computations."""

from binaryninja import ILSourceLocation

from ...utils.log import log_info


_SSA_SET_VAR_OPS = ("MLIL_SET_VAR_SSA", "MLIL_SET_VAR_SSA_FIELD")
_SSA_CLEANUP_OPS = _SSA_SET_VAR_OPS + ("MLIL_VAR_PHI",)


def _ssa_def(ssa, var):
    if not hasattr(var, "version"):
        return None
    try:
        return ssa.get_ssa_var_definition(var)
    except Exception:  # noqa: BLE001
        return None


def _ssa_uses(ssa, var):
    if not hasattr(var, "version"):
        return []
    try:
        return ssa.get_ssa_var_uses(var)
    except Exception:  # noqa: BLE001
        return []


def _is_stack_var(var):
    var = getattr(var, "var", var)
    source_type = getattr(var, "source_type", None)
    return getattr(source_type, "name", str(source_type)) == "StackVariableSourceType"


def _use_escapes(ssa, use, candidates, seen):
    if use.instr_index in candidates:
        return False
    if use.operation.name != "MLIL_VAR_PHI":
        return True
    if use.instr_index in seen:
        return False

    seen.add(use.instr_index)
    for var in use.vars_written:
        for downstream in _ssa_uses(ssa, var):
            if _use_escapes(ssa, downstream, candidates, seen):
                return True
    return False


def _candidate_slice(ssa, root_indices):
    by_index = {ins.instr_index: ins for ins in ssa.instructions}
    by_non_ssa = {}
    for ins in ssa.instructions:
        non_ssa = getattr(ins, "non_ssa_form", None)
        if non_ssa is not None:
            by_non_ssa.setdefault(non_ssa.instr_index, []).append(ins)

    pending = []
    for idx in root_indices:
        mapped = by_non_ssa.get(idx)
        if mapped:
            pending.extend(ins.instr_index for ins in mapped)
        elif idx in by_index:
            pending.append(idx)
    candidates = set()

    while pending:
        idx = pending.pop()
        if idx in candidates:
            continue
        ins = by_index.get(idx)
        if ins is None or ins.operation.name not in _SSA_CLEANUP_OPS:
            continue
        if any(_is_stack_var(var) for var in getattr(ins, "vars_written", ())):
            continue
        candidates.add(idx)
        for var in ins.vars_read:
            definition = _ssa_def(ssa, var)
            if definition is not None:
                pending.append(definition.instr_index)

    return candidates, by_index


def _drop_live_escapes(ssa, candidates, by_index):
    changed = True
    while changed:
        changed = False
        for idx in list(candidates):
            ins = by_index[idx]
            escapes = False
            for var in ins.vars_written:
                if any(_use_escapes(ssa, use, candidates, set()) for use in _ssa_uses(ssa, var)):
                    escapes = True
                    break
            if escapes:
                candidates.remove(idx)
                changed = True
    return candidates


def cleanup_decode(mlil, root_indices, phase_name):
    """NOP dead pure assignments in the decode slice rooted at ``root_indices``."""
    root_indices = set(root_indices or ())
    if mlil is None or not root_indices:
        return 0

    ssa = mlil.ssa_form
    candidates, by_index = _candidate_slice(ssa, root_indices)
    candidates = _drop_live_escapes(ssa, candidates, by_index)

    applied = 0
    done = set()
    for idx in sorted(candidates):
        ins = by_index[idx]
        if ins.operation.name not in _SSA_SET_VAR_OPS:
            continue
        non_ssa = ins.non_ssa_form
        if non_ssa is None or non_ssa.instr_index in done:
            continue
        done.add(non_ssa.instr_index)
        mlil.replace_expr(
            non_ssa.expr_index,
            mlil.nop(ILSourceLocation.from_instruction(non_ssa)),
        )
        applied += 1

    if applied:
        mlil.finalize()
        mlil.generate_ssa_form()
        log_info(f"[phase-cleanup] {phase_name}: NOP'd {applied} decode instruction(s)")
    return applied
