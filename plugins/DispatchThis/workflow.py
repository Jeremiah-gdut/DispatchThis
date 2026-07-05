"""Workflow activity callbacks for DispatchThis."""

from binaryninja import AnalysisContext

from .passes.medium.deflatten import apply_redirections_il, compute_redirections
from .passes.medium.nop_pass import nop_deflatten_state_writes
from .passes.medium.indirect_calls import apply_indirect_call_rewrites
from .passes.medium.branch_conditions import translate_indirect_branch_conditions
from .passes.medium.phase_cleanup import cleanup_decode, set_roots_before
from .passes.medium.global_constants import CONST_SLOT_TYPE
from .passes.low.gadget_llil import (
    apply_llil_jump_rewrites,
    clear_resolved_indirect_branch_tags,
    iter_llil_indirect_jumps,
)
from .profiles import active_profile
from .utils.log import log_info, log_warn, log_debug, log_error
from .workflow_state import FunctionWorkflowState


GLOBAL_CONSTANT_RECEIPTS = "dispatchthis_global_constant_slots"


def _commit_mlil(ctx, mlil):
    try:
        ctx.mlil = mlil
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx.set_mlil_function(mlil)
    except Exception as e:  # noqa: BLE001
        func = ctx.function
        log_warn(f"[workflow] {func.name}: failed to commit MLIL changes: {e}")


def _schedule_tag_cleanup(bv, func_start):
    pending = bv.session_data.setdefault("dispatchthis_tag_cleanup_pending", set())
    if func_start in pending:
        return
    pending.add(func_start)

    def clear_after_analysis():
        try:
            func = bv.get_function_at(func_start)
            if func is not None:
                clear_resolved_indirect_branch_tags(func)
        except Exception as e:  # noqa: BLE001
            log_error(f"[workflow] tag cleanup @ {hex(func_start)}: {e}")
        finally:
            pending.discard(func_start)

    bv.add_analysis_completion_event(clear_after_analysis)


def _normalized_type_name(type_value):
    return str(type_value).replace(" ", "")


def _global_slot_type(bv):
    parsed, _ = bv.parse_type_string(f"{CONST_SLOT_TYPE} dispatchthis_global_constant_slot")
    return parsed


def _global_type_applied(bv, slot_addr):
    data_var = bv.get_data_var_at(slot_addr)
    return data_var is not None and _normalized_type_name(data_var.type) == _normalized_type_name(CONST_SLOT_TYPE)


def _type_is_noreturn(type_):
    return "__noreturn" in str(type_).lower()


def _call_has_fallthrough(mlil, call_il):
    block = call_il.il_basic_block
    for idx in range(block.start, block.end - 1):
        if mlil[idx].instr_index == call_il.instr_index:
            return True
    return bool(block.outgoing_edges)


def _submit_branch_mutations(bv, func, state, resolved_targets):
    mutations = state.branch_updates_for(resolved_targets)
    for source, targets in mutations.items():
        try:
            func.set_user_indirect_branches(source, [(bv.arch, target) for target in targets])
            changed = state.mark_branch_applied(source, targets)
            if changed:
                log_warn(f"[workflow] {func.name}: branch targets changed at {hex(source)}")
        except Exception as e:  # noqa: BLE001
            log_warn(f"[workflow] {func.name}: failed to set branch targets @ {hex(source)}: {e}")
    return mutations


def _resolve_pending_branches_from_function_llil(ctx, state):
    func = ctx.function
    bv = ctx.view
    llil = getattr(func, "low_level_il", None)
    if llil is None:
        return False

    plan = active_profile(bv).resolve_branch_gadget(bv, llil, state.branch_targets())
    resolved_targets = {item["source"]: item["targets"] for item in plan}
    mutations = _submit_branch_mutations(bv, func, state, resolved_targets)
    if mutations:
        bv.session_data.setdefault("dispatchthis_llil_stable", {}).pop(func.start, None)
        log_info(f"[workflow] {func.name}: submitted {len(mutations)} pending indirect branch target update(s)")
        return True

    if not FunctionWorkflowState.unmapped_unresolved_sources(func):
        state.mark_branch_stable()
        bv.session_data.setdefault("dispatchthis_llil_stable", {})[func.start] = True
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)
    return False


def resolve_jumps_llil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    state = FunctionWorkflowState(func)

    if bv.arch.name != "aarch64":
        log_debug(f"[dispatchthis] {func.name}: skipping non-aarch64 view")
        return

    llil_stable = bv.session_data.setdefault("dispatchthis_llil_stable", {})
    if state.branch_stable(func):
        llil_stable[func.start] = True
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)
        return

    log_info(f"[dispatchthis] resolve_llil invoked @ {func.start:#x}")
    llil = ctx.llil
    jump_sources = {jump.address for jump in iter_llil_indirect_jumps(llil)}
    mapped = {branch.source_addr for branch in getattr(func, "indirect_branches", ())}
    func_llil = getattr(func, "low_level_il", None)
    plan_llil = func_llil if mapped and func_llil is not None else llil
    profile = active_profile(bv)
    plan = profile.resolve_branch_gadget(bv, plan_llil, state.branch_targets())
    if plan_llil is llil:
        apply_llil_jump_rewrites(bv, llil, plan)

    covered = {item["source"] for item in plan} | mapped
    if jump_sources - covered and plan_llil is not llil:
        seen_sources = {item["source"] for item in plan}
        context_plan = profile.resolve_branch_gadget(bv, llil, state.branch_targets())
        apply_llil_jump_rewrites(bv, llil, context_plan)
        for item in context_plan:
            if item["source"] not in seen_sources:
                plan.append(item)
                seen_sources.add(item["source"])

    resolved_targets = {item["source"]: item["targets"] for item in plan}
    mutations = _submit_branch_mutations(bv, func, state, resolved_targets)

    log_info(f"[dispatchthis] resolve_llil @ {func.start:#x}: submitted {len(mutations)} branch mutation(s)")
    if mutations:
        llil_stable.pop(func.start, None)
        log_info(f"[workflow] {func.name}: submitted {len(mutations)} indirect branch target update(s)")
        return

    covered = set(resolved_targets) | mapped
    if (
        not FunctionWorkflowState.unmapped_unresolved_sources(func)
        and jump_sources <= covered
    ):
        log_info(f"All of {func.name}'s indirect jumps have been resolved")
        state.mark_branch_stable()
        llil_stable[func.start] = True
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)


def resolve_calls_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    state = FunctionWorkflowState(func)

    if not state.branch_stable(func):
        if bv.arch.name == "aarch64":
            _resolve_pending_branches_from_function_llil(ctx, state)
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    plans = active_profile(bv).resolve_call_gadget(bv, mlil)
    rewrites = apply_indirect_call_rewrites(bv, mlil, plans)
    adjustments = 0
    for plan in plans:
        call_addr = plan["call_addr"]
        target = plan["target"]
        state.mark_call_target(call_addr, target)
        if not state.call_adjustment_needed(call_addr, target):
            continue
        callee = bv.get_function_at(target)
        if callee is None or callee.type is None:
            continue
        if _type_is_noreturn(callee.type) and _call_has_fallthrough(mlil, plan["call_il"]):
            state.mark_call_adjusted(call_addr, target)
            log_warn(
                f"[workflow] {func.name}: skipped noreturn type adjustment at "
                f"{hex(call_addr)} -> {callee.name}; call has fallthrough"
            )
            continue
        try:
            func.set_call_type_adjustment(call_addr, callee.type)
            changed = state.mark_call_adjusted(call_addr, target)
            if changed:
                log_warn(f"[workflow] {func.name}: call target changed at {hex(call_addr)}")
            adjustments += 1
        except Exception as e:  # noqa: BLE001
            log_warn(f"[workflow] {func.name}: type-adjust @ {hex(call_addr)} failed: {e}")

    if not adjustments:
        state.mark_call_stable()
        cleanup_roots = set()
        for plan in plans:
            cleanup_roots.update(plan["cleanup_roots"])
        call_sites = {plan["call_addr"] for plan in plans} or (
            set(state.call_receipts) | set(state.call_target_receipts)
        )
        cleanup_roots.update(set_roots_before(mlil, call_sites))
        cleaned = cleanup_decode(mlil, cleanup_roots, "call")
        state.mark_call_cleanup_done()
        if rewrites or cleaned:
            _commit_mlil(ctx, mlil)
    if rewrites or adjustments:
        log_info(
            f"[workflow] {func.name}: resolved {rewrites} indirect call(s), "
            f"submitted {adjustments} type adjustment(s)"
        )


def translate_branches_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        return

    state = FunctionWorkflowState(func)
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    _, n, cleanup_roots = translate_indirect_branch_conditions(bv, mlil)
    if n:
        log_info(f"[workflow] {func.name}: translated {n} indirect branch condition(s)")
    cleanup_roots.update(set_roots_before(mlil, state.branch_receipts))
    cleaned = cleanup_decode(mlil, cleanup_roots, "branch")
    state.mark_branch_cleanup_done()
    if n or cleaned:
        _commit_mlil(ctx, mlil)


def resolve_globals_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        return

    state = FunctionWorkflowState(func)
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    plans = active_profile(bv).plan_global_constant_slots(bv, mlil)
    if not plans:
        return

    receipts = bv.session_data.setdefault(GLOBAL_CONSTANT_RECEIPTS, {})
    slot_type = None
    applied = 0
    for plan in plans:
        slot_addr = plan["slot_addr"]
        type_name = plan["type"]
        if receipts.get(slot_addr) == type_name and _global_type_applied(bv, slot_addr):
            continue
        if _global_type_applied(bv, slot_addr):
            receipts[slot_addr] = type_name
            continue
        try:
            if slot_type is None:
                slot_type = _global_slot_type(bv)
            bv.define_user_data_var(slot_addr, slot_type)
            if not _global_type_applied(bv, slot_addr):
                log_warn(f"[workflow] {func.name}: failed to verify global const slot @ {hex(slot_addr)}")
                continue
            receipts[slot_addr] = type_name
            applied += 1
        except Exception as e:  # noqa: BLE001
            log_warn(f"[workflow] {func.name}: global const slot @ {hex(slot_addr)} failed: {e}")

    if applied:
        log_info(f"[workflow] {func.name}: typed {applied} global constant slot(s)")


def deflatten_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    mlil = ctx.mlil
    if mlil is None:
        return

    # Eligibility (the Deflatten per-function toggle) gates whether this activity
    # runs at all; by the time we're here the function is enrolled in deflatten.

    # Don't deflatten until the LLIL pass has drained every indirect jump;
    # otherwise the CFG is still incomplete and the dispatcher cluster may be partial.
    llil_stable = bv.session_data.setdefault("dispatchthis_llil_stable", {})
    if not llil_stable.get(func.start):
        return

    redirections = compute_redirections(bv, func, mlil=mlil)
    if not redirections:
        return

    # Stash state tokens and variables so cleanup can NOP state writes. The
    # cleanup pass still consumes integer constants, so keep the value component
    # here; deflatten matching itself uses full (value, width) tokens.
    state_tokens = set()
    state_vars = set()
    for plan in redirections:
        state_tokens.update(plan.get("state_tokens", ()))
        state_vars.update(plan.get("state_vars", ()))
    state_consts = {value for value, _size in state_tokens}
    bv.session_data.setdefault("dispatchthis_state_consts", {})[func.start] = state_consts
    bv.session_data.setdefault("dispatchthis_state_vars", {})[func.start] = state_vars
    log_info(f"[workflow] {func.name}: recorded {len(state_tokens)} dispatcher state token(s)")

    applied = apply_redirections_il(mlil, redirections)

    if applied:
        _commit_mlil(ctx, mlil)
        mlil_stable = bv.session_data.setdefault("dispatchthis_mlil_stable", {})
        log_info(f"{func.name} has been deflattened")
        mlil_stable[func.start] = True


def cleanup_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    mlil = ctx.mlil
    if mlil is None:
        return

    # Skip until deflatten has stabilized; reapply every pass since MLIL rewrites are reverted by each regeneration.
    mlil_stable = bv.session_data.setdefault("dispatchthis_mlil_stable", {})
    if not mlil_stable.get(func.start):
        log_debug(f"[workflow] {func.name}: deflattener has not run yet, skipping cleanup")
        return

    state_writes = nop_deflatten_state_writes(bv, func, mlil=mlil)
    if state_writes:
        _commit_mlil(ctx, mlil)

    log_info(f"{func.name} has been cleaned")
