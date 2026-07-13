"""Phase-owned cleanup for resolved target decode computations."""

from binaryninja import (
    ILSourceLocation,
    MediumLevelILOperation as M,
    VariableSourceType,
)

from ...helpers.mlil import walk_expr
from ...utils.log import log_info


_SSA_SET_VAR_OPS = {M.MLIL_SET_VAR_SSA, M.MLIL_SET_VAR_SSA_FIELD}
_SSA_CLEANUP_OPS = _SSA_SET_VAR_OPS | {M.MLIL_VAR_PHI}
_LOAD_VALUE_OPS = {
    M.MLIL_LOAD,
    M.MLIL_LOAD_SSA,
    M.MLIL_LOAD_STRUCT,
    M.MLIL_LOAD_STRUCT_SSA,
}

# Cleanup is an overlay optimization, so it must be stricter than ordinary
# dataflow simplification. These value operations cannot access memory, invoke
# code, trap, or expose semantics that Binary Ninja could not model. Unknown or
# newly-added operations fail closed until their behavior is reviewed.
_PURE_VALUE_OPS = {
    M.MLIL_VAR,
    M.MLIL_VAR_FIELD,
    M.MLIL_VAR_SPLIT,
    M.MLIL_VAR_SSA,
    M.MLIL_VAR_SSA_FIELD,
    M.MLIL_VAR_ALIASED,
    M.MLIL_VAR_ALIASED_FIELD,
    M.MLIL_VAR_SPLIT_SSA,
    M.MLIL_ADDRESS_OF,
    M.MLIL_ADDRESS_OF_FIELD,
    M.MLIL_CONST,
    M.MLIL_CONST_DATA,
    M.MLIL_CONST_PTR,
    M.MLIL_EXTERN_PTR,
    M.MLIL_FLOAT_CONST,
    M.MLIL_IMPORT,
    M.MLIL_ADD,
    M.MLIL_ADC,
    M.MLIL_SUB,
    M.MLIL_SBB,
    M.MLIL_AND,
    M.MLIL_OR,
    M.MLIL_XOR,
    M.MLIL_LSL,
    M.MLIL_LSR,
    M.MLIL_ASR,
    M.MLIL_ROL,
    M.MLIL_RLC,
    M.MLIL_ROR,
    M.MLIL_RRC,
    M.MLIL_MUL,
    M.MLIL_MULU_DP,
    M.MLIL_MULS_DP,
    M.MLIL_NEG,
    M.MLIL_NOT,
    M.MLIL_SX,
    M.MLIL_ZX,
    M.MLIL_LOW_PART,
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
    M.MLIL_TEST_BIT,
    M.MLIL_BOOL_TO_INT,
    M.MLIL_ADD_OVERFLOW,
}


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
        return None


def _is_stack_var(var):
    var = getattr(var, "var", var)
    return getattr(var, "source_type", None) == VariableSourceType.StackVariableSourceType


def _assignment_is_pure(instruction, allow_load=False):
    """Return whether removing an unused assignment cannot remove behavior."""
    source = getattr(instruction, "src", None)
    if source is None:
        return False
    try:
        nodes = walk_expr(source)
    except (AttributeError, TypeError):
        return False
    allowed = _PURE_VALUE_OPS | _LOAD_VALUE_OPS if allow_load else _PURE_VALUE_OPS
    return bool(nodes) and all(getattr(node, "operation", None) in allowed for node in nodes)


def _use_escapes(ssa, use, candidates, seen):
    if use.instr_index in candidates:
        return False
    if use.operation != M.MLIL_VAR_PHI:
        return True
    if use.instr_index in seen:
        return False

    seen.add(use.instr_index)
    for var in use.vars_written:
        downstream_uses = _ssa_uses(ssa, var)
        if downstream_uses is None:
            return True
        for downstream in downstream_uses:
            if _use_escapes(ssa, downstream, candidates, seen):
                return True
    return False


def _candidate_slice(ssa, root_indices, removable_load_roots=None):
    removable_load_roots = set(removable_load_roots or ())
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
        if ins is None or ins.operation not in _SSA_CLEANUP_OPS:
            continue
        if ins.operation in _SSA_SET_VAR_OPS:
            non_ssa = getattr(ins, "non_ssa_form", None)
            non_ssa_index = getattr(non_ssa, "instr_index", None)
            if not _assignment_is_pure(
                ins,
                allow_load=non_ssa_index in removable_load_roots,
            ):
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
                uses = _ssa_uses(ssa, var)
                if uses is None or any(
                    _use_escapes(ssa, use, candidates, set())
                    for use in uses
                ):
                    escapes = True
                    break
            if escapes:
                candidates.remove(idx)
                changed = True
    return candidates


def _cleanup_assignments(mlil, root_indices, removable_load_roots):
    """Return current non-SSA assignments that phase cleanup may NOP."""
    root_indices = set(root_indices or ())
    removable_load_roots = set(removable_load_roots or ())
    if mlil is None or not root_indices:
        return {}

    ssa = mlil.ssa_form
    candidates, by_index = _candidate_slice(
        ssa,
        root_indices,
        removable_load_roots,
    )
    candidates = _drop_live_escapes(ssa, candidates, by_index)

    assignments = {}
    for idx in sorted(candidates):
        ins = by_index[idx]
        if ins.operation not in _SSA_SET_VAR_OPS:
            continue
        non_ssa = ins.non_ssa_form
        if non_ssa is None or non_ssa.instr_index in assignments:
            continue
        if not _assignment_is_pure(
            non_ssa,
            allow_load=non_ssa.instr_index in removable_load_roots,
        ):
            continue
        assignments[non_ssa.instr_index] = non_ssa
    return assignments


def _apply_cleanup_assignments(mlil, assignments):
    applied = 0
    for non_ssa in assignments.values():
        mlil.replace_expr(
            non_ssa.expr_index,
            mlil.nop(ILSourceLocation.from_instruction(non_ssa)),
        )
        applied += 1
    if applied:
        mlil.finalize()
        mlil.generate_ssa_form()
    return applied


def cleanup_decode(
    mlil,
    root_indices,
    phase_name,
    removable_load_roots=None,
):
    """NOP dead pure assignments in the decode slice rooted at ``root_indices``."""
    assignments = _cleanup_assignments(mlil, root_indices, removable_load_roots)
    applied = _apply_cleanup_assignments(mlil, assignments)
    if applied:
        log_info(f"[phase-cleanup] {phase_name}: NOP'd {applied} decode instruction(s)")
    return applied


def settle_cleanup_decode(
    mlil,
    root_indices,
    phase_name,
    removable_load_roots=None,
):
    """Replan current MLIL until no phase-owned assignment remains.

    The loop is local to one MLIL overlay and never carries instruction indices
    across reanalysis. A repeated plan, failed replacement, or instruction-count
    bound stays fail-closed.
    """
    total = 0
    seen_plans = set()
    try:
        instruction_count = len(mlil)
    except TypeError:
        instruction_count = len(getattr(mlil.ssa_form, "instructions", ()))
    max_passes = max(1, instruction_count, len(set(root_indices or ())) + 1)
    for _ in range(max_passes):
        assignments = _cleanup_assignments(mlil, root_indices, removable_load_roots)
        if not assignments:
            if total:
                log_info(f"[phase-cleanup] {phase_name}: NOP'd {total} decode instruction(s)")
            return total, True
        plan = tuple(assignments)
        if plan in seen_plans:
            return total, False
        seen_plans.add(plan)
        applied = _apply_cleanup_assignments(mlil, assignments)
        if not applied:
            return total, False
        total += applied
    return total, False
