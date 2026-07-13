"""Workflow activity callbacks for DispatchThis."""

from collections.abc import Mapping

from binaryninja import AnalysisContext, FunctionType, Settings, SettingsScope

from .passes.medium.deflatten import rewrite_redirections_mlil
from .passes.medium.correlated_stores import apply_correlated_stores_mlil
from .passes.medium.indirect_calls import (
    apply_indirect_call_rewrites,
    current_call_receipt_plans,
    validate_current_call_plans,
)
from .passes.medium.branch_conditions import translate_indirect_branch_conditions
from .passes.medium.phase_cleanup import settle_cleanup_decode
from .passes.medium.string_decrypt import apply_decrypted_string_comments
from .helpers.mlil import set_roots_before
from .passes.low.gadget_llil import (
    apply_llil_jump_rewrites,
    clear_resolved_indirect_branch_tags,
    iter_llil_indirect_jumps,
    validate_current_branch_plans,
)
from .profiles import active_profile
from .utils.log import log_info, log_warn, log_debug, log_error
from .workflow_state import FunctionWorkflowState


_ANALYSIS_SETTINGS = (
    ("analysis.limits.maxFunctionSize", 0),
    ("analysis.limits.expressionValueComputeMaxDepth", 99999),
    ("analysis.limits.maxFunctionAnalysisTime", 1800000),
    ("analysis.limits.maxFunctionUpdateCount", 1024),
    ("analysis.outlining.builtins", False),
)
_DEFLATTEN_SETTING = "analysis.plugins.dispatchThis.deflatten"


def _ensure_analysis_settings(func):
    try:
        settings = Settings()
        for key, value in _ANALYSIS_SETTINGS:
            getter = settings.get_bool if isinstance(value, bool) else settings.get_integer
            setter = settings.set_bool if isinstance(value, bool) else settings.set_integer
            if getter(key, func) == value:
                continue
            if not setter(key, value, func, SettingsScope.SettingsResourceScope):
                log_warn(f"[workflow] {func.name}: failed to set {key}")
                return False
        for key, value in _ANALYSIS_SETTINGS:
            getter = settings.get_bool if isinstance(value, bool) else settings.get_integer
            if getter(key, func) != value:
                log_warn(f"[workflow] {func.name}: failed to verify {key}")
                return False
    except Exception as e:  # noqa: BLE001
        log_warn(f"[workflow] {func.name}: failed to configure analysis settings: {e}")
        return False
    return True


def _commit_mlil(ctx, mlil):
    try:
        ctx.set_mlil_function(mlil)
        return True
    except Exception as e:  # noqa: BLE001
        func = ctx.function
        log_warn(f"[workflow] {func.name}: failed to commit MLIL changes: {e}")
        return False


def _deflatten_enabled(func):
    try:
        return Settings().get_bool(_DEFLATTEN_SETTING, func)
    except Exception as e:  # noqa: BLE001
        log_warn(f"[workflow] {func.name}: failed to read deflatten setting: {e}")
        return False


def _clear_deflatten_stability(bv, func):
    bv.session_data.get("dispatchthis_mlil_stable", {}).pop(func.start, None)


def branch_cleanup_current(mlil, state):
    """Whether this MLIL overlay has no branch decode assignments left to clean."""
    if not state.branch_cleanup_needed():
        return True
    if not state.branch_cleanup_overlay_ready():
        return False
    return not set_roots_before(
        mlil,
        state.branch_receipts,
    )


def _apply_deflatten(ctx, bv, func, profile, mlil):
    redirections = profile.plan_deflatten_redirections(bv, func, mlil)
    if not redirections:
        return False

    new_mlil, applied = rewrite_redirections_mlil(ctx, mlil, redirections)
    if new_mlil is None or applied != len(redirections) or not _commit_mlil(ctx, new_mlil):
        return False

    bv.session_data.setdefault("dispatchthis_mlil_stable", {})[func.start] = True
    log_info(f"{func.name} has been deflattened")
    return True


def _active_profile_state(bv, func):
    """Bind one callback run to one profile and its function-scoped state."""
    try:
        profile = active_profile(bv)
        return profile, FunctionWorkflowState(func, profile.id)
    except Exception as e:  # noqa: BLE001
        log_warn(f"[workflow] {func.name}: resolver profile state unavailable: {e}")
        return None, None


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


def _global_slot_type(bv, type_name):
    parsed, _ = bv.parse_type_string(f"{type_name} dispatchthis_global_constant_slot")
    return parsed


def _global_type_applied(bv, slot_addr, type_name):
    data_var = bv.get_data_var_at(slot_addr)
    if data_var is None:
        return False
    try:
        return data_var.type == _global_slot_type(bv, type_name)
    except Exception:  # noqa: BLE001
        return False


def _global_plan_consensus(plans):
    """Validate one atomic profile result and collapse exact duplicates."""
    by_slot = {}
    for plan in plans or ():
        if not isinstance(plan, Mapping):
            return None
        slot_addr = plan.get("slot_addr")
        type_name = plan.get("type")
        if type(slot_addr) is not int or slot_addr < 0 or not isinstance(type_name, str) or not type_name:
            return None
        previous = by_slot.setdefault(slot_addr, type_name)
        if previous != type_name:
            return None
    return [{"slot_addr": addr, "type": by_slot[addr]} for addr in sorted(by_slot)]


def _global_receipts_verified(bv, state):
    return state.global_receipts_verified(lambda slot_addr, type_name: _global_type_applied(bv, slot_addr, type_name))


def _type_is_noreturn(type_):
    can_return = getattr(type_, "can_return", None)
    return can_return is not None and not bool(can_return)


def _call_has_fallthrough(mlil, call_il):
    block = call_il.il_basic_block
    for idx in range(block.start, block.end - 1):
        if mlil[idx].instr_index == call_il.instr_index:
            return True
    return bool(block.outgoing_edges)


def _call_adjustment_type(mlil, call_il, callee):
    type_ = getattr(callee, "type", None)
    if type_ is None:
        return True, None
    try:
        call_parameters = tuple(call_il.params)
        parameter_types = tuple(parameter.expr_type for parameter in call_parameters)
        if any(parameter_type is None for parameter_type in parameter_types):
            return True, None
        can_return = (
            True
            if _type_is_noreturn(type_) and _call_has_fallthrough(mlil, call_il)
            else type_.can_return
        )
        adjusted = FunctionType.create(
            ret=type_.return_value,
            params=parameter_types,
            calling_convention=type_.calling_convention,
            variable_arguments=type_.has_variable_arguments,
            stack_adjust=type_.stack_adjustment,
            platform=type_.platform,
            confidence=type_.confidence,
            can_return=can_return,
            pure=type_.pure,
        )
    except Exception:  # noqa: BLE001
        return False, None
    return True, adjusted


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


def _converge_branches(
    ctx,
    state,
    profile,
    plan_llil,
    coverage_llil,
    rewrite_llil=False,
    announce_stable=False,
):
    func = ctx.function
    bv = ctx.view
    jump_sources = {jump.address for jump in iter_llil_indirect_jumps(coverage_llil)}
    plan = validate_current_branch_plans(
        bv,
        plan_llil,
        profile.resolve_branch_gadget(bv, plan_llil, state.verified_branch_targets()),
    )
    if rewrite_llil and any(len(item["targets"]) == 1 for item in plan):
        apply_llil_jump_rewrites(bv, plan_llil, plan)

    resolved_targets = {item["source"]: item["targets"] for item in plan}
    mutations = _submit_branch_mutations(bv, func, state, resolved_targets)
    if mutations:
        return mutations

    mapped = {branch.source_addr for branch in getattr(func, "indirect_branches", ())}
    covered = set(resolved_targets) | mapped
    if not FunctionWorkflowState.unmapped_unresolved_sources(func) and jump_sources <= covered:
        if announce_stable:
            log_info(f"All of {func.name}'s indirect jumps have been resolved")
        state.mark_branch_stable()
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)
    return mutations


def _resolve_pending_branches(ctx, state, profile):
    func = ctx.function
    llil = ctx.llil
    if llil is None:
        return False

    mutations = _converge_branches(ctx, state, profile, llil, llil)
    if mutations:
        log_info(f"[workflow] {func.name}: submitted {len(mutations)} pending indirect branch target update(s)")
        return True
    return False


def resolve_jumps_llil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        log_debug(f"[dispatchthis] {func.name}: skipping non-aarch64 view")
        return
    if not _ensure_analysis_settings(func):
        return

    profile, state = _active_profile_state(bv, func)
    if state is None:
        return

    if state.branch_stable(func):
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)
        return

    log_info(f"[dispatchthis] resolve_llil invoked @ {func.start:#x}")
    llil = ctx.llil
    mutations = _converge_branches(
        ctx,
        state,
        profile,
        llil,
        llil,
        rewrite_llil=True,
        announce_stable=True,
    )

    log_info(f"[dispatchthis] resolve_llil @ {func.start:#x}: submitted {len(mutations)} branch mutation(s)")
    if mutations:
        log_info(f"[workflow] {func.name}: submitted {len(mutations)} indirect branch target update(s)")
        return


def resolve_calls_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    if not _ensure_analysis_settings(func):
        return

    profile, state = _active_profile_state(bv, func)
    if state is None:
        return

    if not state.branch_stable(func):
        if bv.arch.name == "aarch64":
            _resolve_pending_branches(ctx, state, profile)
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    plans = validate_current_call_plans(
        mlil,
        profile.resolve_call_gadget(bv, mlil),
    )
    if plans is None:
        state.invalidate_call_stable()
        return

    rewritten_mlil, rewrites = apply_indirect_call_rewrites(ctx, mlil, plans)
    if rewrites != len(plans):
        state.invalidate_call_stable()
        return
    if rewrites:
        mlil = rewritten_mlil

    receipt_targets = dict(state.call_target_receipts)
    planned_targets = {plan["call_addr"]: plan["target"] for plan in plans}
    receipt_targets.update(planned_targets)
    for call_addr, target in state.call_receipts.items():
        if call_addr in planned_targets and planned_targets[call_addr] != target:
            continue
        previous = receipt_targets.setdefault(call_addr, target)
        if previous != target:
            state.invalidate_call_stable()
            return
    planned_addresses = {plan["call_addr"] for plan in plans}
    receipt_plans = current_call_receipt_plans(
        mlil,
        {
            call_addr: target
            for call_addr, target in receipt_targets.items()
            if call_addr not in planned_addresses
        },
    )
    if receipt_plans is None:
        state.invalidate_call_stable()
        return
    calls = [*plans, *receipt_plans]

    adjustments = 0
    adjustment_failed = False
    for plan in calls:
        call_addr = plan["call_addr"]
        target = plan["target"]
        callee = bv.get_function_at(target)
        safe, adjust_type = _call_adjustment_type(mlil, plan["call_il"], callee)
        if not safe:
            state.invalidate_call_stable()
            adjustment_failed = True
            continue
        if adjust_type is None:
            continue
        if not state.call_adjustment_needed(call_addr, adjust_type):
            continue
        try:
            func.set_call_type_adjustment(call_addr, adjust_type)
            if state.call_adjustment_needed(call_addr, adjust_type):
                log_warn(
                    f"[workflow] {func.name}: failed to verify type adjustment "
                    f"at {hex(call_addr)}"
                )
                adjustment_failed = True
                continue
            adjustments += 1
        except Exception as e:  # noqa: BLE001
            log_warn(f"[workflow] {func.name}: type-adjust @ {hex(call_addr)} failed: {e}")
            adjustment_failed = True

    if adjustment_failed:
        for plan in plans:
            state.mark_call_target(plan["call_addr"], plan["target"])
        state.invalidate_call_stable()
        return

    if adjustments:
        for plan in calls:
            state.mark_call_target(plan["call_addr"], plan["target"])
            state.mark_call_adjusted(plan["call_addr"], plan["target"])
        return

    cleanup_proven = all(plan.get("cleanup_proven", False) for plan in plans)
    cleaned = 0
    settled = True
    if plans and cleanup_proven:
        cleanup_roots = set()
        removable_load_roots = set()
        for plan in plans:
            cleanup_roots.update(plan["cleanup_roots"])
            removable_load_roots.update(plan.get("cleanup_load_roots", ()))
        cleanup_options = (
            {"removable_load_roots": removable_load_roots}
            if removable_load_roots
            else {}
        )
        cleaned, settled = settle_cleanup_decode(
            mlil,
            cleanup_roots,
            "call",
            **cleanup_options,
        )
    for plan in calls:
        changed = state.mark_call_target(plan["call_addr"], plan["target"])
        changed |= state.mark_call_adjusted(plan["call_addr"], plan["target"])
        if changed:
            log_warn(f"[workflow] {func.name}: call target changed at {hex(plan['call_addr'])}")
    if not cleanup_proven or not settled:
        state.invalidate_call_cleanup()
    if not settled:
        state.invalidate_call_stable()
        log_warn(f"[workflow] {func.name}: call cleanup did not settle")
        return
    state.mark_call_stable()
    if receipt_plans or not cleanup_proven or cleaned:
        state.invalidate_call_cleanup()
    elif state.call_cleanup_needed():
        state.mark_call_cleanup_done()
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
    if not _ensure_analysis_settings(func):
        return

    deflatten_enabled = _deflatten_enabled(func)
    if deflatten_enabled:
        _clear_deflatten_stability(bv, func)

    _, state = _active_profile_state(bv, func)
    if state is None:
        return
    # A current-overlay fixed-point exception is valid only for this translator
    # attempt. Never let it survive a fresh MLIL generation or a failed cleanup.
    state.clear_branch_cleanup_overlay_ready()
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return
    if not state.global_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    new_mlil, n, cleanup_roots = translate_indirect_branch_conditions(bv, ctx, mlil)
    if new_mlil is None:
        return
    if n:
        log_info(f"[workflow] {func.name}: translated {n} indirect branch condition(s)")
        # Binary Ninja requires an intermediate copy-transform to be installed
        # before a second MLIL transform can use correct mappings.
        if not _commit_mlil(ctx, new_mlil):
            return
        # The Python binding can still expose the pre-transform value through
        # ``ctx.mlil`` in this callback. The installed copy is the documented
        # input to the next transform.
        mlil = new_mlil
    cleaned = 0
    settled = True
    branch_cleanup_needed = state.branch_cleanup_needed()
    if branch_cleanup_needed or state.branch_receipts or cleanup_roots:
        cleanup_roots.update(set_roots_before(mlil, state.branch_receipts))
        cleaned, settled = settle_cleanup_decode(mlil, cleanup_roots, "branch")
    if not settled:
        state.invalidate_branch_cleanup()
    elif cleaned:
        state.mark_branch_cleanup_overlay_ready()
    elif branch_cleanup_needed:
        state.mark_branch_cleanup_done()


def resolve_globals_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        return
    if not _ensure_analysis_settings(func):
        return

    profile, state = _active_profile_state(bv, func)
    if state is None:
        return
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    plans = _global_plan_consensus(profile.plan_global_constant_slots(bv, mlil))
    if plans is None:
        state.invalidate_globals()
        log_warn(f"[workflow] {func.name}: rejected conflicting or malformed global plan")
        return
    if not plans:
        if _global_receipts_verified(bv, state):
            state.mark_global_stable()
        else:
            state.invalidate_globals()
        return

    # A current plan is unmet evidence until every requested view-level type is
    # verified.  Never carry a previous stable receipt across a failed attempt.
    state.invalidate_globals()

    slot_types = {}
    applied = 0
    changed = False
    failed = False
    for plan in plans:
        slot_addr = plan["slot_addr"]
        type_name = plan["type"]
        if (
            state.global_receipts.get(slot_addr) == type_name
            and _global_type_applied(bv, slot_addr, type_name)
        ):
            continue
        if _global_type_applied(bv, slot_addr, type_name):
            changed = state.mark_global_slot(slot_addr, type_name) or changed
            continue
        try:
            if type_name not in slot_types:
                slot_types[type_name] = _global_slot_type(bv, type_name)
            slot_type = slot_types[type_name]
            bv.define_user_data_var(slot_addr, slot_type)
            if not _global_type_applied(bv, slot_addr, type_name):
                log_warn(f"[workflow] {func.name}: failed to verify global const slot @ {hex(slot_addr)}")
                failed = True
                continue
            state.mark_global_slot(slot_addr, type_name)
            applied += 1
        except Exception as e:  # noqa: BLE001
            failed = True
            log_warn(f"[workflow] {func.name}: global const slot @ {hex(slot_addr)} failed: {e}")

    if _global_receipts_verified(bv, state):
        if not changed and not applied and not failed:
            state.mark_global_stable()
    else:
        state.invalidate_globals()
    if applied:
        log_info(f"[workflow] {func.name}: typed {applied} global constant slot(s)")


def recover_phi_stores_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    if bv.arch.name != "aarch64" or not _ensure_analysis_settings(func):
        return

    profile, state = _active_profile_state(bv, func)
    if state is None:
        return
    if not state.branch_stable(func) or not state.call_stable() or not state.global_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return
    plans = profile.plan_correlated_store_rewrites(bv, func, mlil)
    new_mlil, applied = apply_correlated_stores_mlil(ctx, mlil, plans)
    if applied and new_mlil is not None and _commit_mlil(ctx, new_mlil):
        log_info(f"[workflow] {func.name}: recovered {applied} correlated store(s)")


def string_decrypt_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        return 0
    if not _ensure_analysis_settings(func):
        return 0

    profile, state = _active_profile_state(bv, func)
    if state is None:
        return 0
    if not state.branch_stable(func):
        return 0
    if not state.call_stable():
        return 0
    if not state.global_stable():
        return 0

    mlil_stable = bv.session_data.get("dispatchthis_mlil_stable", {})
    facts = profile.plan_string_decrypt_calls(bv, func, ctx.mlil, mlil_stable)
    annotated = apply_decrypted_string_comments(func, facts)
    if annotated:
        state.invalidate_cleanup()
    return annotated


def deflatten_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    if bv.session_data.get("dispatchthis_mlil_stable", {}).get(func.start):
        return

    mlil = ctx.mlil
    if mlil is None:
        return
    if not _ensure_analysis_settings(func):
        return

    # Eligibility (the Deflatten per-function toggle) gates whether this activity
    # runs at all; by the time we're here the function is enrolled in deflatten.

    # Don't deflatten until the LLIL pass has drained every indirect jump;
    # otherwise the CFG is still incomplete and the dispatcher cluster may be partial.
    profile, state = _active_profile_state(bv, func)
    if state is None:
        return
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return
    if not state.global_stable():
        return
    if state.call_cleanup_needed():
        return
    if not branch_cleanup_current(mlil, state):
        return
    _apply_deflatten(ctx, bv, func, profile, mlil)
