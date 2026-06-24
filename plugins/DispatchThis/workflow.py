"""Workflow activity callbacks for DispatchThis."""

from binaryninja import AnalysisContext

from .passes.medium.deflatten import apply_redirections_il, compute_redirections
from .passes.medium.nop_pass import clean_resolved_gadget_jumps
from .passes.medium.indirect_calls import patch_indirect_calls
from .utils import StateMachine
from .passes.low.gadget_llil import resolve_and_rewrite_llil_jumps
from .utils.log import log_info, log_warn, log_debug


def workflow_resolve_jumps_llil(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view

    log_info(f"[dispatchthis] resolve_llil invoked @ {func.start:#x}")
    llil = analysis_context.llil
    gadget_map = bv.session_data.setdefault("dispatchthis_gadget_map", {})
    func_map = gadget_map.setdefault(func.start, {})
    resolved = resolve_and_rewrite_llil_jumps(bv, llil, func_map)
    log_info(f"[dispatchthis] resolve_llil @ {func.start:#x}: rewrote {len(resolved)} jump(s)")
    if resolved:
        func_map.update(resolved)
        log_info(f"[workflow] {func.name}: rewrote {len(resolved)} indirect jump(s) to direct")
    else:
        llil_stable = bv.session_data.setdefault("dispatchthis_llil_stable", {})
        log_info(f"All of {func.name}'s indirect jumps have been resolved")
        llil_stable[func.start] = True


def workflow_resolve_calls_mlil(analysis_context: AnalysisContext):
    func = analysis_context.function
    bv = analysis_context.view

    mlil = analysis_context.mlil
    if mlil is None:
        return
    n = patch_indirect_calls(bv, mlil)
    if n:
        log_info(f"[workflow] {func.name}: resolved {n} indirect call(s)")


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
