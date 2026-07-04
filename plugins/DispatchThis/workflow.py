"""Workflow activity callbacks for DispatchThis."""

from binaryninja import AnalysisContext

from .passes.medium.deflatten import apply_redirections_il, compute_redirections
from .passes.medium.nop_pass import clean_resolved_gadget_jumps
from .passes.medium.indirect_calls import apply_indirect_call_rewrites, plan_indirect_calls
from .passes.medium.branch_conditions import translate_indirect_branch_conditions
from .passes.medium.phase_cleanup import cleanup_phase_decode, mlil_set_var_roots_before_sites
from .utils import StateMachine
from .passes.low.gadget_llil import (
    apply_llil_jump_rewrites,
    clear_resolved_indirect_branch_tags,
    resolve_llil_jump_plan,
    schedule_resolved_indirect_branch_tag_cleanup,
)
from .utils.log import log_info, log_warn, log_debug
from .workflow_state import FunctionWorkflowState


def _legacy_gadget_value(targets):
    return targets[0] if len(targets) == 1 else tuple(targets)


def _mirror_branch_state_for_legacy_passes(bv, func, state):
    gadget_map = bv.session_data.setdefault("dispatchthis_gadget_map", {}).setdefault(func.start, {})
    gadget_map.clear()
    for source, targets in state.branch_target_receipts().items():
        gadget_map[source] = _legacy_gadget_value(targets)


def workflow_resolve_jumps_llil(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view
    state = FunctionWorkflowState(func)

    if bv.arch.name != "aarch64":
        log_debug(f"[dispatchthis] {func.name}: skipping non-aarch64 view")
        return

    llil_stable = bv.session_data.setdefault("dispatchthis_llil_stable", {})
    if state.branch_resolving_is_stable(func):
        llil_stable[func.start] = True
        _mirror_branch_state_for_legacy_passes(bv, func, state)
        clear_resolved_indirect_branch_tags(func)
        schedule_resolved_indirect_branch_tag_cleanup(bv, func.start)
        return

    log_info(f"[dispatchthis] resolve_llil invoked @ {func.start:#x}")
    llil = analysis_context.llil
    plan = resolve_llil_jump_plan(bv, llil)
    apply_llil_jump_rewrites(bv, llil, plan)

    resolved_targets = {item["source"]: item["targets"] for item in plan}
    mutations = state.branch_mutations_for(resolved_targets)
    for source, targets in mutations.items():
        try:
            func.set_user_indirect_branches(source, [(bv.arch, target) for target in targets])
            changed = state.mark_branch_mutation_applied(source, targets)
            if changed:
                log_warn(f"[workflow] {func.name}: branch targets changed at {hex(source)}")
        except Exception as e:  # noqa: BLE001
            log_warn(f"[workflow] {func.name}: failed to set branch targets @ {hex(source)}: {e}")

    _mirror_branch_state_for_legacy_passes(bv, func, state)
    log_info(f"[dispatchthis] resolve_llil @ {func.start:#x}: submitted {len(mutations)} branch mutation(s)")
    if mutations:
        llil_stable.pop(func.start, None)
        log_info(f"[workflow] {func.name}: submitted {len(mutations)} indirect branch target update(s)")
        return

    if not FunctionWorkflowState.unmapped_unresolved_sources(func):
        log_info(f"All of {func.name}'s indirect jumps have been resolved")
        state.mark_branch_resolving_stable()
        llil_stable[func.start] = True
        clear_resolved_indirect_branch_tags(func)
        schedule_resolved_indirect_branch_tag_cleanup(bv, func.start)


def workflow_resolve_calls_mlil(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view
    state = FunctionWorkflowState(func)

    if not state.branch_resolving_is_stable(func):
        return

    mlil = analysis_context.mlil
    if mlil is None:
        return

    plans = plan_indirect_calls(bv, mlil)
    rewrites = apply_indirect_call_rewrites(bv, mlil, plans)
    adjustments = 0
    for plan in plans:
        call_addr = plan["call_addr"]
        target = plan["target"]
        if not state.call_adjustment_needed(call_addr, target):
            continue
        callee = bv.get_function_at(target)
        if callee is None or callee.type is None:
            continue
        try:
            func.set_call_type_adjustment(call_addr, callee.type)
            changed = state.mark_call_adjustment_applied(call_addr, target)
            if changed:
                log_warn(f"[workflow] {func.name}: call target changed at {hex(call_addr)}")
            adjustments += 1
        except Exception as e:  # noqa: BLE001
            log_warn(f"[workflow] {func.name}: type-adjust @ {hex(call_addr)} failed: {e}")

    if not adjustments:
        state.mark_indirect_call_resolving_stable()
        if state.call_cleanup_needed():
            cleanup_roots = set()
            for plan in plans:
                cleanup_roots.update(plan["cleanup_roots"])
            call_sites = {plan["call_addr"] for plan in plans} or set(state.call_receipts)
            cleanup_roots.update(mlil_set_var_roots_before_sites(mlil, call_sites))
            cleanup_phase_decode(mlil, cleanup_roots, "call")
            state.mark_call_cleanup_done()
    if rewrites or adjustments:
        log_info(
            f"[workflow] {func.name}: resolved {rewrites} indirect call(s), "
            f"submitted {adjustments} type adjustment(s)"
        )


def workflow_translate_branches_mlil(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view

    if bv.arch.name != "aarch64":
        return

    state = FunctionWorkflowState(func)
    if not state.branch_resolving_is_stable(func):
        return

    mlil = analysis_context.mlil
    if mlil is None:
        return

    _, n, cleanup_roots = translate_indirect_branch_conditions(bv, mlil)
    if n:
        log_info(f"[workflow] {func.name}: translated {n} indirect branch condition(s)")
    if state.branch_cleanup_needed():
        cleanup_roots.update(mlil_set_var_roots_before_sites(mlil, state.branch_receipts))
        cleanup_phase_decode(mlil, cleanup_roots, "branch")
        state.mark_branch_cleanup_done()


def workflow_deflatten_mlil(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view
    mlil = func.mlil
    if mlil is None:
        return

    # Eligibility (the Deflatten per-function toggle) gates whether this activity
    # runs at all; by the time we're here the function is enrolled in deflatten.

    # Don't deflatten until the LLIL pass has drained every indirect jump --
    # otherwise the CFG is still incomplete and the state machine is partial.
    llil_stable = bv.session_data.setdefault("dispatchthis_llil_stable", {})
    if not llil_stable.get(func.start):
        return

    # MLIL rewrites are overlays on LLIL and reverted on each regeneration, so deflatten re-applies every pass.
    sm = StateMachine(bv, func).analyze()
    if sm.state_var is None:
        return

    # {jump_addr: target} recovered by the LLIL pass that resolved indirect jumps
    gadget_map = bv.session_data.get("dispatchthis_gadget_map", {}).get(func.start, {})
    if not gadget_map:
        log_warn(f"[workflow] {func.name}: no resolved gadget map; nothing to deflatten")
        return

    # Stash state constants and variable aliases so cleanup can precisely NOP state writes.
    state_consts = set(sm.backbone.keys())
    bv.session_data.setdefault("dispatchthis_state_consts", {})[func.start] = state_consts
    bv.session_data.setdefault("dispatchthis_state_vars", {})[func.start] = sm.state_write_vars
    log_info(f"[workflow] {func.name}: recorded {len(state_consts)} dispatcher state constant(s)")

    redirections = compute_redirections(bv, func, sm=sm, gadget_map=gadget_map)
    applied = apply_redirections_il(func.medium_level_il, redirections) if redirections else 0

    if applied:
        mlil_stable = bv.session_data.setdefault("dispatchthis_mlil_stable", {})
        log_info(f"{func.name} has been deflattened")
        mlil_stable[func.start] = True


def workflow_cleanup(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view
    mlil = func.mlil
    if mlil is None:
        return

    # Skip until deflatten has stabilized; reapply every pass since MLIL rewrites are reverted by each regeneration.
    mlil_stable = bv.session_data.setdefault("dispatchthis_mlil_stable", {})
    if not mlil_stable.get(func.start):
        log_debug(f"[workflow] {func.name}: deflattener has not run yet, skipping cleanup")
        return

    # Convert remaining gadget jumps to gotos and NOP dead decode gadgets.
    clean_resolved_gadget_jumps(bv, func)

    log_info(f"{func.name} has been cleaned")
