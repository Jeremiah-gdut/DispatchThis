"""Workflow activity callbacks for DispatchThis."""  # noqa: F401  # noqa: SIZE_OK — AGENTS.md reserves reanalysis mutation orchestration for this module.

from binaryninja import AnalysisContext, FunctionType, Settings, SettingsScope, Type

from .passes.medium.deflatten import rewrite_redirections_mlil
from .passes.medium.correlated_stores import apply_correlated_stores_mlil
from .passes.medium.indirect_calls import (
    apply_indirect_call_rewrites,
    current_call_receipt_plans,
    validate_current_call_facts,
    validate_current_call_plans,
)
from .passes.medium.branch_conditions import (
    ConditionFailureReason,
    ConditionReceipt,
    ConditionTranslationStatus,
    capture_condition_receipt,
    clear_condition_failure_tags,
    publish_condition_failure_tag,
    translate_indirect_branch_conditions,
)
from .passes.medium.phase_cleanup import settle_cleanup_decode
from .passes.medium.string_decrypt import apply_decrypted_string_comments
from .helpers.mlil import iter_indirect_calls, set_roots_before
from .passes.low.gadget_llil import (
    apply_llil_jump_rewrites,
    clear_resolved_indirect_branch_tags,
    iter_llil_indirect_jumps,
    validate_current_branch_plans,
)
from .providers import (
    ProviderBindingError,
    _legacy_profile,
    _pending_reproof_functions,
    _set_pending_reproof_functions,
    active_provider,
)
from .semantics import (
    BranchTargetFact,
    BranchTargetQuery,
    CallTargetFact,
    CallTargetQuery,
    CompleteBatch,
    CorrelatedStorePlan,
    CorrelatedStoreQuery,
    GlobalDataFact,
    GlobalDataQuery,
    Inconclusive,
)
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
    except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja settings boundary.
        log_warn(f"[workflow] {func.name}: failed to configure analysis settings: {e}")
        return False
    return True


def _commit_mlil(ctx, mlil):
    try:
        ctx.set_mlil_function(mlil)
        return True
    except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja workflow boundary.
        func = ctx.function
        log_warn(f"[workflow] {func.name}: failed to commit MLIL changes: {e}")
        return False


def _deflatten_enabled(func):
    try:
        return Settings().get_bool(_DEFLATTEN_SETTING, func)
    except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja settings boundary.
        log_warn(f"[workflow] {func.name}: failed to read deflatten setting: {e}")
        return False


def _clear_deflatten_stability(bv, func):
    bv.session_data.get("dispatchthis_mlil_stable", {}).pop(func.start, None)


def branch_cleanup_current(mlil, state):
    """Whether this MLIL overlay has no branch decode assignments left to clean."""
    if not state.conditions_complete():
        return False
    if not state.branch_cleanup_needed():
        return True
    if not state.branch_cleanup_overlay_ready():
        return False
    return not set_roots_before(
        mlil,
        state.branch_cleanup_overlay_sources(),
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


def _active_provider_state(bv, func):
    """Bind one callback run to the explicit view provider and function state."""
    try:
        provider = active_provider(bv)
    except ProviderBindingError as error:
        log_warn(f"[workflow] {func.name}: provider binding unavailable: {error}")
        return None, None, None
    legacy = _legacy_profile(provider.provider_id)
    pending_reproof = _pending_reproof_functions(bv)
    return (
        provider,
        legacy,
        FunctionWorkflowState(
            func,
            seed_legacy_branch_receipts=(
                legacy is not None and pending_reproof is not None and func.start not in pending_reproof
            ),
        ),
    )


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
        except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — deferred Binary Ninja callback boundary.
            log_error(f"[workflow] tag cleanup @ {hex(func_start)}: {e}")
        finally:
            pending.discard(func_start)

    bv.add_analysis_completion_event(clear_after_analysis)


def _same_type(left, right):
    try:
        return left == right
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja type comparison boundary.
        return False


def _global_type_applied(bv, slot_addr, data_type):
    try:
        data_var = bv.get_data_var_at(slot_addr)
        if data_var is None:
            return False
        return _same_type(data_var.type, data_type)
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja data-variable readback boundary.
        return False


def _mapped_type_range(bv, slot_addr, width):
    end = slot_addr + width
    if end <= slot_addr:
        return False
    current = slot_addr
    while current < end:
        try:
            segment = bv.get_segment_at(current)
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja segment lookup boundary.
            return False
        start = getattr(segment, "start", None)
        segment_end = getattr(segment, "end", None)
        if (
            segment is None
            or type(start) is not int
            or type(segment_end) is not int
            or start > current
            or segment_end <= current
        ):
            return False
        current = min(end, segment_end)
    return True


def _global_fact_consensus(bv, facts):
    """Validate an atomic native-type batch and collapse exact duplicates."""
    plans = []
    for fact in facts:
        slot_addr = fact.slot_addr
        data_type = fact.data_type
        width = getattr(data_type, "width", None)
        if (
            type(slot_addr) is not int
            or slot_addr < 0
            or not isinstance(data_type, Type)
            or type(width) is not int
            or width <= 0
            or not _mapped_type_range(bv, slot_addr, width)
        ):
            return None
        previous = next((plan for plan in plans if plan[0] == slot_addr), None)
        if previous is not None:
            if not _same_type(previous[1], data_type):
                return None
            continue
        plans.append((slot_addr, data_type, width))
    plans.sort(key=lambda plan: plan[0])
    for previous, current in zip(plans, plans[1:]):
        if current[0] < previous[0] + previous[2]:
            return None
    return [(slot_addr, data_type) for slot_addr, data_type, _width in plans]


def _global_receipts_verified(bv, state):
    return state.global_receipts_verified(lambda slot_addr, data_type: _global_type_applied(bv, slot_addr, data_type))


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
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja type construction boundary.
        return False, None
    return True, adjusted


def _provider_call_plans(bv, func, mlil, provider):
    """Validate one complete external call-target scan before mutation."""
    slot = getattr(provider, "call_targets", None)
    if slot is None:
        log_debug(f"[workflow] {func.name}: provider does not implement call target recovery")
        return [], frozenset(), frozenset()
    try:
        result = slot(CallTargetQuery(bv, func, mlil))
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — provider boundary must fail closed.
        log_warn(f"[workflow] {func.name}: call provider failed: {error}")
        return None
    if type(result) is Inconclusive:
        log_warn(f"[workflow] {func.name}: call provider was inconclusive: {result.reason}")
        return None
    if (
        type(result) is not CompleteBatch
        or type(result.facts) is not tuple
        or any(type(fact) is not CallTargetFact for fact in result.facts)
    ):
        log_warn(f"[workflow] {func.name}: call provider returned an invalid batch")
        return None
    try:
        facts = validate_current_call_facts(
            mlil,
            [(fact.call_il, fact.targets) for fact in result.facts],
        )
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — current-IL validation must fail closed at the provider boundary.
        log_warn(f"[workflow] {func.name}: call batch validation failed: {error}")
        return None
    if facts is None:
        return None
    plans = [
        {
            "call_il": call_il,
            "call_addr": call_il.address,
            "target": targets[0],
            "decode_def": None,
        }
        for call_il, targets in facts
        if len(targets) == 1
    ]
    try:
        plans = validate_current_call_plans(mlil, plans)
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — current-IL validation must fail closed at the provider boundary.
        log_warn(f"[workflow] {func.name}: singleton call planning failed: {error}")
        return None
    if plans is None:
        return None
    return plans, frozenset(
        call_il.address
        for call_il, targets in facts
        if len(targets) > 1
    ), frozenset(call_il.address for call_il, _targets in facts)


def _clear_unsupported_call_adjustments(func, state, call_addresses):
    """Remove core-owned singleton overrides displaced by unsupported call facts."""
    mutated = False
    for call_addr in call_addresses:
        adjust_type = state.discard_call_site(call_addr)
        if adjust_type is None:
            continue
        try:
            if not _same_type(func.get_call_type_adjustment(call_addr), adjust_type):
                continue
            func.set_call_type_adjustment(call_addr, None)
            if func.get_call_type_adjustment(call_addr) is not None:
                log_warn(
                    f"[workflow] {func.name}: failed to clear type adjustment "
                    f"at {hex(call_addr)}"
                )
                return None
            mutated = True
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja call-adjustment mutation boundary.
            log_warn(
                f"[workflow] {func.name}: failed to clear type adjustment "
                f"at {hex(call_addr)}: {error}"
            )
            return None
    return mutated


def _provider_global_plans(bv, func, mlil, provider):
    """Validate one complete external global-data scan before mutation."""
    slot = getattr(provider, "global_data", None)
    if slot is None:
        log_debug(f"[workflow] {func.name}: provider does not implement global data recovery")
        return []
    try:
        result = slot(GlobalDataQuery(bv, func, mlil))
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — provider boundary must fail closed.
        log_warn(f"[workflow] {func.name}: global provider failed: {error}")
        return None
    if type(result) is Inconclusive:
        log_warn(f"[workflow] {func.name}: global provider was inconclusive: {result.reason}")
        return None
    if (
        type(result) is not CompleteBatch
        or type(result.facts) is not tuple
        or any(type(fact) is not GlobalDataFact for fact in result.facts)
    ):
        log_warn(f"[workflow] {func.name}: global provider returned an invalid batch")
        return None
    try:
        plans = _global_fact_consensus(bv, result.facts)
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — native Type boundaries must fail closed.
        log_warn(f"[workflow] {func.name}: global batch validation failed: {error}")
        return None
    if plans is None:
        log_warn(f"[workflow] {func.name}: rejected conflicting or malformed global batch")
    return plans


def _provider_correlated_store_plans(bv, func, mlil, provider):
    """Validate one complete external correlated-STORE scan before mutation."""
    slot = getattr(provider, "correlated_stores", None)
    if slot is None:
        log_debug(f"[workflow] {func.name}: provider does not implement correlated STORE recovery")
        return []
    try:
        result = slot(CorrelatedStoreQuery(bv, func, mlil))
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — provider boundary must fail closed.
        log_warn(f"[workflow] {func.name}: correlated STORE provider failed: {error}")
        return None
    if type(result) is Inconclusive:
        log_warn(f"[workflow] {func.name}: correlated STORE provider was inconclusive: {result.reason}")
        return None
    if (
        type(result) is not CompleteBatch
        or type(result.facts) is not tuple
        or any(type(plan) is not CorrelatedStorePlan for plan in result.facts)
    ):
        log_warn(f"[workflow] {func.name}: correlated STORE provider returned an invalid batch")
        return None
    return result.facts


def _provider_branch_plan(bv, func, llil, provider):
    """Validate one external branch batch before the workflow mutates BN state."""
    resolver = provider.branch_targets
    if resolver is None:
        log_debug(f"[workflow] {func.name}: provider does not implement branch target recovery")
        return None
    if llil is None:
        return None
    try:
        result = resolver(BranchTargetQuery(bv, func, llil))
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — provider boundary must reject every failure without receipts.
        log_warn(f"[workflow] {func.name}: branch provider failed: {error}")
        return None
    if type(result) is Inconclusive:
        log_warn(f"[workflow] {func.name}: branch provider was inconclusive: {result.reason}")
        return None
    if type(result) is not CompleteBatch:
        log_warn(f"[workflow] {func.name}: branch provider returned an invalid batch")
        return None

    if type(result.facts) is not tuple:
        log_warn(f"[workflow] {func.name}: branch provider returned malformed facts")
        return None

    plans = []
    sources = set()
    for fact in result.facts:
        if type(fact) is not BranchTargetFact:
            log_warn(f"[workflow] {func.name}: branch provider returned an unsupported fact")
            return None
        jump_il = fact.jump_il
        targets = fact.targets
        condition_receipt = None
        if fact.condition is None:
            if fact.true_target is not None or fact.false_target is not None:
                log_warn(f"[workflow] {func.name}: branch provider returned an unsupported fact")
                return None
        else:
            condition_receipt = capture_condition_receipt(
                llil,
                getattr(jump_il, "address", None),
                fact.condition,
                fact.true_target,
                fact.false_target,
            )
            if condition_receipt is None:
                log_warn(f"[workflow] {func.name}: branch provider returned an unanchorable condition")
                return None
        source = getattr(jump_il, "address", None)
        dest_expr_index = getattr(getattr(jump_il, "dest", None), "expr_index", None)
        if (
            type(targets) is not tuple
            or not targets
            or any(type(target) is not int or target < 0 for target in targets)
            or tuple(sorted(set(targets))) != targets
            or type(source) is not int
            or source < 0
            or type(dest_expr_index) is not int
            or dest_expr_index < 0
            or source in sources
        ):
            log_warn(f"[workflow] {func.name}: branch provider returned a malformed fact")
            return None
        sources.add(source)
        plan = {
            "source": source,
            "dest_expr_index": dest_expr_index,
            "targets": targets,
            "jump_il": jump_il,
        }
        if condition_receipt is not None:
            plan["condition_receipt"] = condition_receipt
        plans.append(plan)

    try:
        validated = validate_current_branch_plans(bv, llil, plans)
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — current-IL validation must reject every batch failure atomically.
        log_warn(f"[workflow] {func.name}: branch batch validation failed: {error}")
        return None
    if (
        type(validated) is not list
        or len(validated) != len(plans)
        or {id(plan) for plan in validated} != {id(plan) for plan in plans}
    ):
        log_warn(f"[workflow] {func.name}: branch provider batch was stale or conflicting")
        return None
    return validated


def _legacy_branch_plan(bv, llil, state, profile):
    """Keep bundled profiles on their private migration path."""
    try:
        plans = profile.resolve_branch_gadget(bv, llil, state.verified_branch_targets())
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — legacy plugin boundary.
        log_warn(f"[workflow] legacy branch planner failed: {error}")
        return None
    return validate_current_branch_plans(bv, llil, plans)


def _provider_reproof_settled(bv, func, state, resolved_targets):
    """Clear a binding-change guard only after the new provider proves all metadata."""
    pending = _pending_reproof_functions(bv)
    if pending is None:
        log_warn(f"[workflow] {func.name}: provider branch reproof state is malformed")
        return False
    if func.start not in pending:
        return True
    if all(
        resolved_targets.get(source) == targets
        for source, targets in state.current_user_branch_targets().items()
    ):
        if not _set_pending_reproof_functions(bv, pending - {func.start}):
            log_warn(f"[workflow] {func.name}: failed to clear provider branch reproof guard")
            return False
    updated = _pending_reproof_functions(bv)
    return updated is not None and func.start not in updated


def _handoff_condition_receipt(func, state, plan):
    receipt = plan.get("condition_receipt")
    changed = state.set_condition_receipt(
        plan["source"],
        None if receipt is None else receipt.as_data(),
    )
    if changed:
        clear_condition_failure_tags(func, (plan["source"],))
    return changed


def _submit_branch_mutations(bv, func, state, plans):
    submitted = {}
    attempted = False
    for plan in plans:
        source = plan["source"]
        targets = plan["targets"]
        needs_update = source in state.branch_updates_for({source: targets})
        if not needs_update:
            _handoff_condition_receipt(func, state, plan)
            continue
        if state.branch_metadata_matches(source, targets):
            changed = state.mark_branch_applied(source, targets)
            if changed:
                clear_condition_failure_tags(func, (source,))
            _handoff_condition_receipt(func, state, plan)
            if changed:
                log_warn(f"[workflow] {func.name}: branch targets changed at {hex(source)}")
            continue
        attempted = True
        try:
            func.set_user_indirect_branches(source, [(bv.arch, target) for target in targets])
            if not state.branch_metadata_matches(source, targets):
                log_warn(
                    f"[workflow] {func.name}: branch target readback did not match "
                    f"at {hex(source)}"
                )
                continue
            changed = state.mark_branch_applied(source, targets)
            if changed:
                clear_condition_failure_tags(func, (source,))
            _handoff_condition_receipt(func, state, plan)
            submitted[source] = targets
            if changed:
                log_warn(f"[workflow] {func.name}: branch targets changed at {hex(source)}")
        except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — the only core-owned indirect-branch mutation boundary.
            log_warn(f"[workflow] {func.name}: failed to set branch targets @ {hex(source)}: {e}")
    return submitted, attempted


def _converge_branches(
    ctx,
    state,
    provider,
    plan_llil,
    coverage_llil,
    rewrite_llil=False,
    announce_stable=False,
    current_plan=None,
    legacy=None,
):
    func = ctx.function
    bv = ctx.view
    jump_sources = {jump.address for jump in iter_llil_indirect_jumps(coverage_llil)}
    plan = current_plan
    if plan is None:
        plan = (
            _legacy_branch_plan(bv, plan_llil, state, legacy)
            if legacy is not None
            else _provider_branch_plan(bv, func, plan_llil, provider)
    )
    if plan is None:
        return {}
    if rewrite_llil and any(len(item["targets"]) == 1 for item in plan):
        apply_llil_jump_rewrites(bv, plan_llil, plan)

    resolved_targets = {item["source"]: item["targets"] for item in plan}
    mutations, attempted = _submit_branch_mutations(bv, func, state, plan)
    reproof_settled = _provider_reproof_settled(bv, func, state, resolved_targets)
    if attempted:
        return mutations

    covered = set(state.verified_branch_targets())
    if (
        reproof_settled
        and not FunctionWorkflowState.unmapped_unresolved_sources(func)
        and jump_sources <= covered
        and set(state.current_user_branch_targets()) <= covered
    ):
        if announce_stable:
            log_info(f"All of {func.name}'s indirect jumps have been resolved")
        state.mark_branch_stable()
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)
    return mutations


def resolve_jumps_llil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        log_debug(f"[dispatchthis] {func.name}: skipping non-aarch64 view")
        return
    provider, legacy, state = _active_provider_state(bv, func)
    if state is None:
        return

    if state.branch_stable(func):
        clear_resolved_indirect_branch_tags(func)
        _schedule_tag_cleanup(bv, func.start)
        return

    log_info(f"[dispatchthis] resolve_llil invoked @ {func.start:#x}")
    llil = ctx.llil
    plan = (
        _legacy_branch_plan(bv, llil, state, legacy)
        if legacy is not None
        else _provider_branch_plan(bv, func, llil, provider)
    )
    if plan is None:
        return
    if not _ensure_analysis_settings(func):
        return
    mutations = _converge_branches(
        ctx,
        state,
        provider,
        llil,
        llil,
        rewrite_llil=True,
        announce_stable=True,
        current_plan=plan,
        legacy=legacy,
    )

    log_info(f"[dispatchthis] resolve_llil @ {func.start:#x}: submitted {len(mutations)} branch mutation(s)")
    if mutations:
        log_info(f"[workflow] {func.name}: submitted {len(mutations)} indirect branch target update(s)")
        return


def resolve_calls_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    provider, _legacy, state = _active_provider_state(bv, func)
    if state is None:
        return

    if not state.branch_stable(func):
        return

    if not _ensure_analysis_settings(func):
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    provider_plans = _provider_call_plans(bv, func, mlil, provider)
    if provider_plans is None:
        state.invalidate_call_stable()
        return
    plans, multi_call_addresses, reported_call_addresses = provider_plans
    try:
        omitted_call_addresses = {
            call_il.address
            for call_il in iter_indirect_calls(mlil)
            if call_il.address not in reported_call_addresses
        }
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — current-MLIL scan must fail closed.
        log_warn(f"[workflow] {func.name}: could not enumerate indirect calls: {error}")
        state.invalidate_call_stable()
        return
    cleared_adjustment = _clear_unsupported_call_adjustments(
        func,
        state,
        multi_call_addresses | omitted_call_addresses,
    )
    if cleared_adjustment is None:
        state.invalidate_call_stable()
        return
    if cleared_adjustment:
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
    adjusted_types = {}
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
            adjusted_types[call_addr] = adjust_type
        except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — the only core-owned call-adjustment mutation boundary.
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
            if plan["call_addr"] in adjusted_types:
                state.mark_call_adjustment(
                    plan["call_addr"],
                    adjusted_types[plan["call_addr"]],
                )
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
    _provider, _legacy, state = _active_provider_state(bv, func)
    if state is None:
        return
    if not _ensure_analysis_settings(func):
        return

    deflatten_enabled = _deflatten_enabled(func)
    if deflatten_enabled:
        _clear_deflatten_stability(bv, func)
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

    receipts = []
    for source, data in state.condition_receipts.items():
        receipt = ConditionReceipt.from_data(source, data)
        if receipt is None:
            state.invalidate_branch_cleanup()
            log_error(f"[workflow] {func.name}: invalid condition receipt @ {hex(source)}")
            return
        receipts.append(receipt)

    # A fresh translator attempt must never reuse a prior overlay's cleanup or
    # implicit success. Current outcomes below are the only condition evidence.
    state.invalidate_branch_cleanup()
    batch = translate_indirect_branch_conditions(ctx, ctx.llil, mlil, tuple(receipts))

    def record_results(suppress_shared_failure_diagnostics=False):
        for result in batch.results:
            if result.status is ConditionTranslationStatus.FAILED:
                failure = result.failure
                if failure is None:
                    continue
                changed = state.record_condition_failure(result.source, failure.reason.value)
                shared_transform_failure = (
                    suppress_shared_failure_diagnostics
                    and failure.reason
                    in {
                        ConditionFailureReason.COPY_FAILED,
                        ConditionFailureReason.INSTALL_FAILED,
                    }
                )
                if changed and shared_transform_failure:
                    # A copy/install failure has one function-level error. Do
                    # not leave a tag from an earlier, unrelated site failure.
                    clear_condition_failure_tags(func, (result.source,))
                elif changed:
                    publish_condition_failure_tag(bv, func, failure)
                    log_warn(
                        f"[workflow] {func.name}: condition @ {hex(failure.source)} "
                        f"failed ({failure.reason.value}): {failure.detail}"
                    )
                continue
            if state.clear_condition_failure(result.source):
                clear_condition_failure_tags(func, (result.source,))

    if batch.backend_failed:
        record_results(suppress_shared_failure_diagnostics=True)
        state.invalidate_branch_cleanup()
        log_error(f"[workflow] {func.name}: branch-condition transform failed")
        return

    if batch.rewrite_sources:
        if not _commit_mlil(ctx, batch.new_mlil):
            batch = batch.with_rewrite_failure(
                ConditionFailureReason.INSTALL_FAILED,
                "AnalysisContext rejected the atomic branch-condition MLIL install",
            )
            record_results(suppress_shared_failure_diagnostics=True)
            state.invalidate_branch_cleanup()
            log_error(f"[workflow] {func.name}: branch-condition MLIL install failed")
            return
        mlil = batch.new_mlil
        log_info(
            f"[workflow] {func.name}: translated "
            f"{len(batch.rewrite_sources)} indirect branch condition(s)"
        )

    record_results()
    cleaned = 0
    settled = True
    if batch.cleanup_roots:
        cleaned, settled = settle_cleanup_decode(mlil, set(batch.cleanup_roots), "branch")
    # A failed receipt keeps the function-level branch cleanup receipt open even
    # when the successfully installed sites had no further decode assignments.
    if not settled or not state.conditions_complete():
        state.invalidate_branch_cleanup()
    elif cleaned:
        state.mark_branch_cleanup_overlay_ready(batch.rewrite_sources)
    else:
        state.mark_branch_cleanup_done()


def resolve_globals_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        return
    provider, _legacy, state = _active_provider_state(bv, func)
    if state is None:
        return
    if not _ensure_analysis_settings(func):
        return
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return

    plans = _provider_global_plans(bv, func, mlil, provider)
    if plans is None:
        state.invalidate_globals()
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

    applied = 0
    changed = False
    failed = False
    for slot_addr, data_type in plans:
        if (
            _same_type(state.global_receipts.get(slot_addr), data_type)
            and _global_type_applied(bv, slot_addr, data_type)
        ):
            continue
        if _global_type_applied(bv, slot_addr, data_type):
            changed = state.mark_global_slot(slot_addr, data_type) or changed
            continue
        try:
            bv.define_user_data_var(slot_addr, data_type)
            if not _global_type_applied(bv, slot_addr, data_type):
                log_warn(f"[workflow] {func.name}: failed to verify global data slot @ {hex(slot_addr)}")
                failed = True
                continue
            state.mark_global_slot(slot_addr, data_type)
            applied += 1
        except Exception as e:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja global-data mutation boundary.
            failed = True
            log_warn(f"[workflow] {func.name}: global data slot @ {hex(slot_addr)} failed: {e}")

    if _global_receipts_verified(bv, state):
        if not changed and not applied and not failed:
            state.mark_global_stable()
    else:
        state.invalidate_globals()
    if applied:
        log_info(f"[workflow] {func.name}: typed {applied} global data slot(s)")


def recover_phi_stores_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view
    if bv.arch.name != "aarch64":
        return

    provider, _profile, state = _active_provider_state(bv, func)
    if provider is None or state is None:
        return
    if not _ensure_analysis_settings(func):
        return
    if not state.branch_stable(func) or not state.call_stable() or not state.global_stable():
        return

    mlil = ctx.mlil
    if mlil is None:
        return
    plans = _provider_correlated_store_plans(bv, func, mlil, provider)
    if plans is None:
        return
    new_mlil, applied = apply_correlated_stores_mlil(ctx, mlil, plans)
    if applied and new_mlil is not None and _commit_mlil(ctx, new_mlil):
        log_info(f"[workflow] {func.name}: recovered {applied} correlated store(s)")


def string_decrypt_mlil(ctx: AnalysisContext):
    func = ctx.function
    bv = ctx.view

    if bv.arch.name != "aarch64":
        return 0
    _provider, profile, state = _active_provider_state(bv, func)
    if state is None:
        return 0
    if profile is None:
        return 0
    if not _ensure_analysis_settings(func):
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

    # Eligibility (the Deflatten per-function toggle) gates whether this activity
    # runs at all; by the time we're here the function is enrolled in deflatten.

    # Don't deflatten until the LLIL pass has drained every indirect jump;
    # otherwise the CFG is still incomplete and the dispatcher cluster may be partial.
    _provider, profile, state = _active_provider_state(bv, func)
    if state is None:
        return
    if profile is None:
        return
    if not _ensure_analysis_settings(func):
        return
    if not state.branch_stable(func):
        return
    if not state.call_stable():
        return
    if not state.global_stable():
        return
    if not state.conditions_complete():
        return
    if state.call_cleanup_needed():
        return
    if not branch_cleanup_current(mlil, state):
        return
    _apply_deflatten(ctx, bv, func, profile, mlil)
