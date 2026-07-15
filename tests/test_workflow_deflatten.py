import types

from binaryninja import FunctionType, Type

from conftest import load_plugin_module, temporary_modules


semantics = load_plugin_module("plugins.DispatchThis.semantics")
branch_conditions = load_plugin_module("plugins.DispatchThis.passes.medium.branch_conditions")

calls = []
branch_plan_calls = []
branch_plan_results = {}
call_plan_calls = []
call_plan_results = []
call_rewrite_calls = []
receipt_plan_parameters = {}
global_plan_calls = []
global_plan_results = []
correlated_plan_calls = []
correlated_plan_results = []
correlated_rewrite_calls = []
correlated_rewrite_results = []
active_profile_calls = []
branch_iter_items = []
clear_tag_calls = []
cleanup_decode_calls = []
cleanup_decode_results = []
set_roots_before_calls = []
set_roots_before_results = []
string_decrypt_calls = []
string_decrypt_results = []
deflatten_rewrite_results = []


def fake_compute(_bv, func, mlil=None):
    calls.append(("compute", func.start, mlil))
    return [{"kind": "uncond"}]


def fake_rewrite_redirections_mlil(ctx, mlil, plans):
    calls.append(("rewrite", ctx, mlil, plans))
    return deflatten_rewrite_results.pop(0) if deflatten_rewrite_results else (mlil, 1)


def fake_resolve_llil_jump_plan(_bv, llil, known_targets=None):
    branch_plan_calls.append((llil, known_targets))
    return branch_plan_results.get(llil, [])


def fake_resolve_call_gadget(bv, mlil):
    call_plan_calls.append((bv, mlil))
    return list(call_plan_results)


def fake_call_targets(query):
    call_plan_calls.append((query.view, query.mlil))
    return semantics.CompleteBatch(tuple(
        semantics.CallTargetFact(plan["call_il"], (plan["target"],))
        for plan in call_plan_results
    ))


def fake_apply_indirect_call_rewrites(ctx, mlil, plans):
    call_rewrite_calls.append((ctx, mlil, plans))
    return mlil, len(plans)


def _function_type(parameter_count=0, can_return=True):
    return FunctionType.create(
        ret=Type.int(8),
        params=[Type.int(8) for _ in range(parameter_count)],
        can_return=can_return,
    )


def _typed_parameter(type_=None):
    return types.SimpleNamespace(expr_type=type_ or Type.int(8))


def fake_current_call_receipt_plans(_mlil, receipts):
    return [
        {
            "call_il": types.SimpleNamespace(
                address=call_addr,
                params=list(receipt_plan_parameters.get(call_addr, ())),
            ),
            "call_addr": call_addr,
            "target": target,
            "cleanup_roots": set(),
        }
        for call_addr, target in receipts.items()
    ]


def fake_plan_global_constant_slots(bv, mlil):
    global_plan_calls.append((bv, mlil))
    return list(global_plan_results)


def fake_global_plans(bv, _func, mlil, _provider):
    plans = fake_plan_global_constant_slots(bv, mlil)
    by_slot = {}
    for plan in plans:
        previous = by_slot.setdefault(plan["slot_addr"], plan["type"])
        if previous != plan["type"]:
            return None
    return [(slot_addr, by_slot[slot_addr]) for slot_addr in sorted(by_slot)]


def fake_validate_current_call_plans(_mlil, plans):
    extras = {
        plan["call_addr"]: plan
        for plan in call_plan_results
    }
    return [
        {
            **plan,
            **{
                key: value
                for key, value in extras.get(plan["call_addr"], {}).items()
                if key.startswith("cleanup_")
            },
            "cleanup_proven": extras.get(plan["call_addr"], {}).get(
                "cleanup_proven",
                True,
            ),
        }
        for plan in plans
    ]


def fake_correlated_stores(query):
    correlated_plan_calls.append((query.view, query.function, query.mlil))
    return semantics.CompleteBatch(tuple(correlated_plan_results))


def fake_apply_correlated_stores_mlil(ctx, mlil, plans):
    correlated_rewrite_calls.append((ctx, mlil, plans))
    return correlated_rewrite_results.pop(0) if correlated_rewrite_results else (mlil, 0)


def fake_plan_deflatten_redirections(bv, func, mlil):
    calls.append(("compute", func.start, mlil))
    return [{"kind": "uncond"}]


def fake_deflatten(query):
    calls.append(("compute", query.function.start, query.mlil))
    return semantics.CompleteBatch(
        (
            semantics.DeflattenPlan(
                kind=semantics.DeflattenPlanKind.UNCONDITIONAL,
                owner_block=object(),
                exit_redirections=(semantics.DeflattenRedirection(object(), 0x2000),),
                state_token=semantics.DeflattenStateToken(0x1234, 4),
            ),
        )
    )


def fake_string_recovery(query):
    string_decrypt_calls.append(
        (
            query.view,
            query.function,
            query.mlil,
            query.deflattened_function_starts,
        )
    )
    return semantics.CompleteBatch(
        tuple(string_decrypt_results.pop(0) if string_decrypt_results else ())
    )


def fake_apply_decrypted_string_comments(_func, facts):
    return len(facts)


def _condition_batch(mlil, *, results=(), sources=(), roots=(), backend_failed=False):
    return branch_conditions.ConditionTranslationBatch(
        mlil,
        tuple(results),
        frozenset(sources),
        frozenset(roots),
        backend_failed,
    )


def _condition_receipt_data(source):
    return branch_conditions.ConditionReceipt(
        source,
        branch_conditions.ILAnchor(0x900, 1, (("dest", -1),), "LLIL_CMP_NE", 1),
        0x8DB6FC,
        0x8DB700,
    ).as_data()


def fake_settle_cleanup_decode(*args, **kwargs):
    cleanup_decode_calls.append((args, kwargs))
    result = cleanup_decode_results.pop(0) if cleanup_decode_results else 0
    return result if isinstance(result, tuple) else (result, True)


def fake_active_profile(bv):
    active_profile_calls.append(bv)
    return types.SimpleNamespace(
        id="test",
        resolve_branch_gadget=fake_resolve_llil_jump_plan,
        resolve_call_gadget=fake_resolve_call_gadget,
        plan_global_constant_slots=fake_plan_global_constant_slots,
        plan_deflatten_redirections=fake_plan_deflatten_redirections,
    )


_legacy_provider_view = None


class FakeProviderBindingError(RuntimeError):
    pass


def fake_active_provider(bv):
    global _legacy_provider_view
    _legacy_provider_view = bv
    return types.SimpleNamespace(
        provider_id="test",
        call_targets=fake_call_targets,
        correlated_stores=fake_correlated_stores,
        deflatten=fake_deflatten,
        string_recovery=fake_string_recovery,
    )


def fake_legacy_profile(_provider_id):
    return fake_active_profile(_legacy_provider_view)


class FakeWorkflowState:
    receipts = {}
    verified_receipts = None
    unmapped = set()
    marked_stable = False
    stable = False
    updates = {}
    applied = []
    call_targets = []
    call_adjustment_checks = []
    call_receipts = {}
    call_target_receipts = {}
    call_stable_marked = False
    call_cleanup_marked = False
    branch_cleanup_marked = False
    global_receipts = {}
    global_slots = []
    global_stable_marked = False
    globals_stable = False
    calls_stable = False
    cleanup_invalidated = False
    branch_cleanup = True
    branch_cleanup_overlay = False
    branch_cleanup_sources = ()
    call_cleanup = True
    conditions = {}
    condition_failures_data = {}
    conditions_complete_flag = True

    def __init__(self, _func, seed_legacy_branch_receipts=False):
        pass

    @staticmethod
    def unmapped_unresolved_sources(_func):
        return FakeWorkflowState.unmapped

    def branch_stable(self, _func):
        return self.stable

    def branch_targets(self):
        return self.receipts

    def verified_branch_targets(self):
        return self.receipts if self.verified_receipts is None else self.verified_receipts

    def current_user_branch_targets(self):
        return self.receipts

    @property
    def branch_receipts(self):
        return FakeWorkflowState.receipts

    @property
    def condition_receipts(self):
        return FakeWorkflowState.conditions

    @property
    def condition_failures(self):
        return FakeWorkflowState.condition_failures_data

    def branch_updates_for(self, _resolved_targets):
        return self.updates

    def branch_metadata_matches(self, _source, _targets):
        return True

    def mark_branch_stable(self):
        FakeWorkflowState.marked_stable = True

    def branch_cleanup_needed(self):
        return FakeWorkflowState.branch_cleanup

    def mark_branch_cleanup_done(self):
        FakeWorkflowState.branch_cleanup_marked = True
        FakeWorkflowState.branch_cleanup = False
        FakeWorkflowState.branch_cleanup_overlay = False
        FakeWorkflowState.branch_cleanup_sources = ()

    def invalidate_branch_cleanup(self):
        FakeWorkflowState.branch_cleanup = True
        FakeWorkflowState.branch_cleanup_overlay = False
        FakeWorkflowState.branch_cleanup_sources = ()

    def mark_branch_cleanup_overlay_ready(self, sources=()):
        FakeWorkflowState.branch_cleanup = True
        FakeWorkflowState.branch_cleanup_overlay = True
        FakeWorkflowState.branch_cleanup_sources = tuple(sources)

    def clear_branch_cleanup_overlay_ready(self):
        FakeWorkflowState.branch_cleanup_overlay = False
        FakeWorkflowState.branch_cleanup_sources = ()

    def branch_cleanup_overlay_ready(self):
        return FakeWorkflowState.branch_cleanup_overlay

    def branch_cleanup_overlay_sources(self):
        return FakeWorkflowState.branch_cleanup_sources

    def set_condition_receipt(self, source, receipt):
        previous = FakeWorkflowState.conditions.get(source)
        if receipt is None:
            FakeWorkflowState.conditions.pop(source, None)
            FakeWorkflowState.condition_failures_data.pop(source, None)
            return previous is not None
        if previous == receipt:
            return False
        FakeWorkflowState.conditions[source] = receipt
        FakeWorkflowState.condition_failures_data.pop(source, None)
        return True

    def record_condition_failure(self, source, reason):
        if FakeWorkflowState.condition_failures_data.get(source) == reason:
            return False
        FakeWorkflowState.condition_failures_data[source] = reason
        return True

    def clear_condition_failure(self, source):
        return FakeWorkflowState.condition_failures_data.pop(source, None) is not None

    def conditions_complete(self):
        return (
            FakeWorkflowState.conditions_complete_flag
            and not FakeWorkflowState.condition_failures_data
        )

    def mark_branch_applied(self, source, targets):
        FakeWorkflowState.applied.append((source, targets))
        return False

    def mark_call_target(self, call_addr, target):
        stale_adjustment = (
            call_addr in FakeWorkflowState.call_receipts
            and FakeWorkflowState.call_receipts[call_addr] != target
        )
        if stale_adjustment:
            FakeWorkflowState.call_receipts.pop(call_addr)
        if FakeWorkflowState.call_target_receipts.get(call_addr) == target and not stale_adjustment:
            return False
        FakeWorkflowState.call_targets.append((call_addr, target))
        previous = FakeWorkflowState.call_target_receipts.get(call_addr)
        FakeWorkflowState.call_target_receipts[call_addr] = target
        return previous is not None

    def call_adjustment_needed(self, call_addr, adjust_type):
        FakeWorkflowState.call_adjustment_checks.append((call_addr, adjust_type))
        return False

    def mark_call_adjusted(self, call_addr, target):
        previous = FakeWorkflowState.call_receipts.get(call_addr)
        FakeWorkflowState.call_receipts[call_addr] = target
        return previous is not None and previous != target

    def mark_call_adjustment(self, _call_addr, _adjust_type):
        pass

    def mark_call_stable(self):
        FakeWorkflowState.call_stable_marked = True

    def invalidate_call_stable(self):
        FakeWorkflowState.call_stable_marked = False

    def mark_call_cleanup_done(self):
        FakeWorkflowState.call_cleanup_marked = True
        FakeWorkflowState.call_cleanup = False

    def invalidate_call_cleanup(self):
        FakeWorkflowState.call_cleanup = True

    def call_stable(self):
        return FakeWorkflowState.calls_stable

    def call_cleanup_needed(self):
        return FakeWorkflowState.call_cleanup

    def mark_global_slot(self, slot_addr, type_name):
        FakeWorkflowState.global_slots.append((slot_addr, type_name))
        previous = FakeWorkflowState.global_receipts.get(slot_addr)
        FakeWorkflowState.global_receipts[slot_addr] = type_name
        FakeWorkflowState.globals_stable = False
        return previous != type_name

    def mark_global_stable(self):
        FakeWorkflowState.global_stable_marked = True
        FakeWorkflowState.globals_stable = True

    def global_stable(self):
        return FakeWorkflowState.globals_stable

    def global_receipts_verified(self, verifier):
        return all(verifier(slot_addr, type_name) for slot_addr, type_name in FakeWorkflowState.global_receipts.items())

    def invalidate_globals(self):
        FakeWorkflowState.globals_stable = False

    def invalidate_cleanup(self):
        FakeWorkflowState.cleanup_invalidated = True
        FakeWorkflowState.branch_cleanup = True
        FakeWorkflowState.branch_cleanup_overlay = False
        FakeWorkflowState.call_cleanup = True


def forbidden_plan_indirect_calls(*_args, **_kwargs):
    raise AssertionError("workflow call planning must go through the active provider")


def forbidden_plan_global_constant_slots(*_args, **_kwargs):
    raise AssertionError("workflow global planning must go through the active provider")


_FAKE_MODULES = {
    "plugins.DispatchThis.passes.medium.deflatten": types.SimpleNamespace(
        compute_redirections=fake_compute,
        rewrite_redirections_mlil=fake_rewrite_redirections_mlil,
    ),
    "plugins.DispatchThis.passes.medium.correlated_stores": types.SimpleNamespace(
        apply_correlated_stores_mlil=fake_apply_correlated_stores_mlil,
    ),
    "plugins.DispatchThis.passes.medium.indirect_calls": types.SimpleNamespace(
        apply_indirect_call_rewrites=fake_apply_indirect_call_rewrites,
        current_call_receipt_plans=fake_current_call_receipt_plans,
        plan_indirect_calls=forbidden_plan_indirect_calls,
        validate_current_call_facts=lambda _mlil, facts: list(facts),
        validate_current_call_plans=fake_validate_current_call_plans,
    ),
    "plugins.DispatchThis.passes.medium.branch_conditions": types.SimpleNamespace(
        ConditionFailureReason=branch_conditions.ConditionFailureReason,
        ConditionReceipt=branch_conditions.ConditionReceipt,
        ConditionTranslationStatus=branch_conditions.ConditionTranslationStatus,
        capture_condition_receipt=branch_conditions.capture_condition_receipt,
        clear_condition_failure_tags=lambda *_args: None,
        publish_condition_failure_tag=lambda *_args: None,
        translate_indirect_branch_conditions=lambda _ctx, _llil, mlil, _receipts: branch_conditions.ConditionTranslationBatch(
            mlil,
            (),
            frozenset(),
            frozenset(),
        ),
    ),
    "plugins.DispatchThis.passes.medium.phase_cleanup": types.SimpleNamespace(
        settle_cleanup_decode=fake_settle_cleanup_decode,
    ),
    "plugins.DispatchThis.helpers.mlil": types.SimpleNamespace(
        iter_indirect_calls=lambda _mlil: iter(()),
        set_roots_before=lambda *args, **kwargs: (
            set_roots_before_calls.append((args, kwargs)),
            set_roots_before_results.pop(0) if set_roots_before_results else set(),
        )[1],
    ),
    "plugins.DispatchThis.passes.medium.string_decrypt": types.SimpleNamespace(
        apply_decrypted_string_comments=fake_apply_decrypted_string_comments,
    ),
    "plugins.DispatchThis.passes.low.gadget_llil": types.SimpleNamespace(
        apply_llil_jump_rewrites=lambda *_args, **_kwargs: 0,
        clear_resolved_indirect_branch_tags=lambda func: clear_tag_calls.append(func),
        iter_llil_indirect_jumps=lambda _llil: iter(branch_iter_items),
        resolve_llil_jump_plan=fake_resolve_llil_jump_plan,
        validate_current_branch_plans=lambda _bv, _llil, plans: list(plans),
    ),
    "plugins.DispatchThis.profiles": types.SimpleNamespace(
        active_profile=fake_active_profile,
    ),
    "plugins.DispatchThis.providers": types.SimpleNamespace(
        ProviderBindingError=FakeProviderBindingError,
        active_provider=fake_active_provider,
        _legacy_profile=fake_legacy_profile,
        _pending_reproof_functions=lambda _bv: frozenset(),
        _set_pending_reproof_functions=lambda _bv, _starts: True,
    ),
    "plugins.DispatchThis.utils.log": types.SimpleNamespace(
        log_info=lambda _msg: None,
        log_warn=lambda _msg: None,
        log_debug=lambda _msg: None,
        log_error=lambda _msg: None,
    ),
    "plugins.DispatchThis.workflow_state": types.SimpleNamespace(
        FunctionWorkflowState=FakeWorkflowState,
    ),
}

with temporary_modules(_FAKE_MODULES, clear=("plugins.DispatchThis.workflow",)):
    workflow = load_plugin_module("plugins.DispatchThis.workflow")

workflow._provider_global_plans = fake_global_plans


class FakeContext:
    def __init__(self):
        self.function = types.SimpleNamespace(
            start=0x9556D8,
            name="sub_9556d8",
            set_user_indirect_branches=lambda *_args: None,
        )
        self.view = types.SimpleNamespace(
            arch=types.SimpleNamespace(name="aarch64"),
            session_data={},
            get_function_at=lambda _target: None,
        )
        self.typed_globals = []

        def parse_type_string(decl):
            name = "dispatchthis_global_constant_slot"
            if f" {name}" not in decl:
                return (decl, None)
            before, after = decl.split(f" {name}", 1)
            return (f"{before}{after}", name)

        def get_data_var_at(addr):
            return self.view.session_data.setdefault("data_vars", {}).get(addr)

        def define_user_data_var(addr, type_):
            self.typed_globals.append((addr, type_))
            self.view.session_data.setdefault("data_vars", {})[addr] = types.SimpleNamespace(type=type_)

        self.view.parse_type_string = parse_type_string
        self.view.get_data_var_at = get_data_var_at
        self.view.define_user_data_var = define_user_data_var
        self._mlil = object()
        self.llil = types.SimpleNamespace()
        self.committed = False
        self.installed_mlil = None

    @property
    def mlil(self):
        return self._mlil

    @mlil.setter
    def mlil(self, value):
        self.committed = value is self._mlil

    def set_mlil_function(self, mlil):
        self.installed_mlil = mlil
        self.committed = True


class FakeAnalysisSettings:
    def __init__(self, ignore_key=None, fail_key=None):
        self.ignore_key = ignore_key
        self.fail_key = fail_key
        self.values = {}
        self.reads = []
        self.writes = []

    def get_integer(self, key, resource=None):
        self.reads.append(("integer", key, resource))
        return self.values.get((key, id(resource)))

    def get_bool(self, key, resource=None):
        self.reads.append(("bool", key, resource))
        return self.values.get((key, id(resource)))

    def set_integer(self, key, value, resource=None, scope=None):
        return self._set("integer", key, value, resource, scope)

    def set_bool(self, key, value, resource=None, scope=None):
        return self._set("bool", key, value, resource, scope)

    def _set(self, kind, key, value, resource, scope):
        self.writes.append((kind, key, value, resource, scope))
        if key == self.fail_key:
            return False
        if key != self.ignore_key:
            self.values[(key, id(resource))] = value
        return True


def _branch_settings_context():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    active_profile_calls.clear()
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items.clear()
    ctx = FakeContext()
    ctx.llil = object()
    ctx.view.add_analysis_completion_event = lambda _callback: None
    return ctx


def test_branch_resolver_configures_function_scoped_analysis_settings(monkeypatch):
    settings = FakeAnalysisSettings()
    ctx = _branch_settings_context()
    monkeypatch.setattr(workflow, "Settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workflow,
        "SettingsScope",
        types.SimpleNamespace(SettingsResourceScope="function"),
        raising=False,
    )

    workflow.resolve_jumps_llil(ctx)

    expected = [
        ("integer", "analysis.limits.maxFunctionSize", 0, ctx.function, "function"),
        ("integer", "analysis.limits.expressionValueComputeMaxDepth", 99999, ctx.function, "function"),
        ("integer", "analysis.limits.maxFunctionAnalysisTime", 1800000, ctx.function, "function"),
        ("integer", "analysis.limits.maxFunctionUpdateCount", 1024, ctx.function, "function"),
        ("bool", "analysis.outlining.builtins", False, ctx.function, "function"),
    ]
    assert settings.writes == expected
    assert all(settings.reads.count((kind, key, ctx.function)) >= 2 for kind, key, *_ in expected)
    assert active_profile_calls == [ctx.view]

    settings.writes.clear()
    workflow.resolve_jumps_llil(ctx)

    assert settings.writes == []


def test_branch_resolver_stops_before_profile_when_settings_do_not_verify(monkeypatch):
    settings = FakeAnalysisSettings(ignore_key="analysis.limits.maxFunctionSize")
    ctx = _branch_settings_context()
    monkeypatch.setattr(workflow, "Settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workflow,
        "SettingsScope",
        types.SimpleNamespace(SettingsResourceScope="function"),
        raising=False,
    )

    workflow.resolve_jumps_llil(ctx)

    assert settings.writes == [
        ("integer", "analysis.limits.maxFunctionSize", 0, ctx.function, "function"),
        ("integer", "analysis.limits.expressionValueComputeMaxDepth", 99999, ctx.function, "function"),
        ("integer", "analysis.limits.maxFunctionAnalysisTime", 1800000, ctx.function, "function"),
        ("integer", "analysis.limits.maxFunctionUpdateCount", 1024, ctx.function, "function"),
        ("bool", "analysis.outlining.builtins", False, ctx.function, "function"),
    ]
    assert active_profile_calls == [ctx.view]
    assert len(branch_plan_calls) == 1
    assert FakeWorkflowState.marked_stable is False


def test_branch_resolver_stops_before_profile_when_setting_write_fails(monkeypatch):
    settings = FakeAnalysisSettings(fail_key="analysis.limits.maxFunctionSize")
    ctx = _branch_settings_context()
    monkeypatch.setattr(workflow, "Settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workflow,
        "SettingsScope",
        types.SimpleNamespace(SettingsResourceScope="function"),
        raising=False,
    )

    workflow.resolve_jumps_llil(ctx)

    assert settings.writes == [
        ("integer", "analysis.limits.maxFunctionSize", 0, ctx.function, "function"),
    ]
    assert active_profile_calls == [ctx.view]
    assert len(branch_plan_calls) == 1
    assert FakeWorkflowState.marked_stable is False


def test_call_phase_requires_branch_stability_before_settings_or_branch_work(monkeypatch):
    settings = FakeAnalysisSettings(ignore_key="analysis.limits.maxFunctionSize")
    ctx = _branch_settings_context()
    ctx.function.low_level_il = "function-llil"
    branch_plan_results["function-llil"] = [{"source": 0x3000, "targets": (0x4000,)}]
    monkeypatch.setattr(workflow, "Settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workflow,
        "SettingsScope",
        types.SimpleNamespace(SettingsResourceScope="function"),
        raising=False,
    )

    workflow.resolve_calls_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert branch_plan_calls == []
    assert FakeWorkflowState.marked_stable is False


def test_deflatten_workflow_runs_without_branch_mirror_state():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = False
    FakeWorkflowState.call_cleanup = False
    calls.clear()
    active_profile_calls.clear()
    deflatten_rewrite_results.clear()
    ctx = FakeContext()
    replacement = object()
    deflatten_rewrite_results[:] = [(replacement, 1)]
    old_commit = workflow._commit_mlil

    def commit_after_state_is_empty(ctx_arg, mlil_arg):
        assert ctx_arg.view.session_data == {}
        return old_commit(ctx_arg, mlil_arg)

    workflow._commit_mlil = commit_after_state_is_empty

    try:
        workflow.deflatten_mlil(ctx)
    finally:
        workflow._commit_mlil = old_commit

    assert active_profile_calls == [ctx.view]
    assert calls[0] == ("compute", ctx.function.start, ctx.mlil)
    assert calls[1][0] == "rewrite"
    assert calls[1][1] is ctx
    assert calls[1][2] is ctx.mlil
    assert ctx.committed is True
    assert ctx.installed_mlil is replacement
    assert ctx.view.session_data["dispatchthis_mlil_stable"][ctx.function.start] is True
    assert "dispatchthis_llil_stable" not in ctx.view.session_data
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.call_cleanup = True
    deflatten_rewrite_results.clear()


def test_deflatten_retries_without_publishing_state_after_commit_failure():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = False
    FakeWorkflowState.call_cleanup = False
    calls.clear()
    deflatten_rewrite_results.clear()
    ctx = FakeContext()
    first_replacement = object()
    second_replacement = object()
    deflatten_rewrite_results[:] = [(first_replacement, 1), (second_replacement, 1)]
    old_commit = workflow._commit_mlil
    commits = []
    install_results = [False, True]
    workflow._commit_mlil = lambda ctx_arg, mlil_arg: (
        commits.append((ctx_arg, mlil_arg)),
        install_results.pop(0),
    )[1]

    try:
        workflow.deflatten_mlil(ctx)
        assert "dispatchthis_mlil_stable" not in ctx.view.session_data
        workflow.deflatten_mlil(ctx)
    finally:
        workflow._commit_mlil = old_commit

    assert commits == [(ctx, first_replacement), (ctx, second_replacement)]
    assert ctx.view.session_data["dispatchthis_mlil_stable"][ctx.function.start] is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.call_cleanup = True
    deflatten_rewrite_results.clear()


def test_deflatten_waits_for_global_phase_stability():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = False
    calls.clear()
    ctx = FakeContext()

    workflow.deflatten_mlil(ctx)

    assert calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.call_cleanup = True


def test_deflatten_waits_for_branch_cleanup_confirmation():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.call_cleanup = False
    calls.clear()
    set_roots_before_results[:] = [{21621}]

    workflow.deflatten_mlil(FakeContext())

    assert calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = True
    set_roots_before_results.clear()


def test_deflatten_accepts_a_current_branch_cleanup_fixed_point():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_overlay = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    calls.clear()
    deflatten_rewrite_results.clear()
    set_roots_before_results[:] = [set()]
    ctx = FakeContext()
    deflatten_rewrite_results[:] = [(object(), 1)]

    workflow.deflatten_mlil(ctx)

    assert calls[0] == ("compute", ctx.function.start, ctx.mlil)
    assert calls[1][0] == "rewrite"
    assert FakeWorkflowState.branch_cleanup is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.branch_cleanup_overlay = False
    FakeWorkflowState.receipts = {}
    deflatten_rewrite_results.clear()
    set_roots_before_results.clear()


def test_deflatten_requires_a_current_cleanup_overlay_for_empty_roots():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_overlay = False
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    calls.clear()
    set_roots_before_calls.clear()
    set_roots_before_results.clear()
    ctx = FakeContext()

    workflow.deflatten_mlil(ctx)

    assert calls == []
    assert set_roots_before_calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.receipts = {}


def test_failed_branch_cleanup_never_opens_the_current_overlay_gate():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_overlay = False
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_results[:] = [(0, False)]
    calls.clear()
    cleanup_decode_calls.clear()
    set_roots_before_calls.clear()
    set_roots_before_results.clear()
    old_translate = workflow.translate_indirect_branch_conditions
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, mlil, _receipts: _condition_batch(
        mlil,
        sources={0x8DB6F8},
        roots={44},
    )
    ctx = FakeContext()

    try:
        workflow.translate_branches_mlil(ctx)
        workflow.deflatten_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate

    assert cleanup_decode_calls == [((ctx.mlil, {44}, "branch"), {})]
    assert FakeWorkflowState.branch_cleanup_overlay is False
    assert calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.receipts = {}
    cleanup_decode_results.clear()


def test_deflatten_waits_for_call_cleanup_confirmation():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = False
    FakeWorkflowState.call_cleanup = True
    calls.clear()

    workflow.deflatten_mlil(FakeContext())

    assert calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False


def test_branch_translation_revokes_prior_stability_before_a_new_attempt():
    other_function = 0x7000
    old_enabled = workflow._deflatten_enabled
    ctx = FakeContext()
    ctx.view.session_data = {
        "dispatchthis_mlil_stable": {
            ctx.function.start: True,
            other_function: True,
        },
    }

    try:
        FakeWorkflowState.stable = False
        FakeWorkflowState.branch_cleanup_overlay = True
        workflow._deflatten_enabled = lambda _func: True
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow._deflatten_enabled = old_enabled
        FakeWorkflowState.stable = False

    stable = ctx.view.session_data["dispatchthis_mlil_stable"]
    assert ctx.function.start not in stable
    assert stable[other_function] is True
    assert FakeWorkflowState.branch_cleanup_overlay is False


def test_branch_resolver_passes_only_verified_receipts_as_known_targets(monkeypatch):
    FakeWorkflowState.receipts = {
        0x1000: (0x2000, 0x3000),
        0x4000: (0x5000,),
    }
    verified = {0x1000: (0x2000, 0x3000)}
    monkeypatch.setattr(FakeWorkflowState, "verified_receipts", verified)
    FakeWorkflowState.unmapped = {0x1000}
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_plan_results.clear()
    active_profile_calls.clear()
    branch_iter_items.clear()
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = object()

    workflow.resolve_jumps_llil(ctx)

    assert [known_targets for _llil, known_targets in branch_plan_calls] == [verified]
    assert active_profile_calls == [ctx.view]
    assert "dispatchthis_gadget_map" not in ctx.view.session_data


def test_branch_resolver_skips_llil_rewrite_for_multi_target_only_plan(monkeypatch):
    rewrite_calls = []
    plan = [{
        "source": 0x1000,
        "targets": (0x2000, 0x3000),
    }]
    ctx = FakeContext()
    ctx.view.add_analysis_completion_event = lambda _callback: None
    state = FakeWorkflowState(ctx.function)
    profile = types.SimpleNamespace(
        resolve_branch_gadget=lambda *_args: plan,
    )
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.updates = {}
    branch_iter_items.clear()
    monkeypatch.setattr(
        workflow,
        "apply_llil_jump_rewrites",
        lambda *_args: rewrite_calls.append(True),
    )

    workflow._converge_branches(
        ctx,
        state,
        types.SimpleNamespace(),
        "context-llil",
        "context-llil",
        rewrite_llil=True,
        legacy=profile,
    )

    assert rewrite_calls == []


def test_branch_resolver_does_not_stabilize_unparsed_indirect_jumps():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x1000)]
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = object()

    workflow.resolve_jumps_llil(ctx)

    assert [known_targets for _llil, known_targets in branch_plan_calls] == [{}]
    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


def test_branch_resolver_checks_coverage_before_llil_rewrite():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x1000)]
    ctx = FakeContext()
    ctx.llil = "context-llil"
    ctx.view.add_analysis_completion_event = lambda _callback: None
    old_rewrite = workflow.apply_llil_jump_rewrites
    workflow.apply_llil_jump_rewrites = lambda *_args: branch_iter_items.clear()

    try:
        workflow.resolve_jumps_llil(ctx)
    finally:
        workflow.apply_llil_jump_rewrites = old_rewrite

    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


def test_branch_resolver_does_not_stabilize_unparsed_later_jump_after_partial_mapping():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x2000)]
    ctx = FakeContext()
    ctx.function.indirect_branches = [types.SimpleNamespace(source_addr=0x1000)]
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = object()

    workflow.resolve_jumps_llil(ctx)

    assert [known_targets for _llil, known_targets in branch_plan_calls] == [{}]
    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


def test_branch_resolver_uses_context_llil_for_newly_discovered_jump():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = {0x2000}
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x2000)]
    ctx = FakeContext()
    ctx.function.indirect_branches = [types.SimpleNamespace(source_addr=0x1000)]
    ctx.function.low_level_il = "function-llil"
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = "context-llil"
    branch_plan_results.clear()
    branch_plan_results["context-llil"] = [{"source": 0x2000, "targets": (0x3000,)}]

    workflow.resolve_jumps_llil(ctx)

    assert [llil for llil, _known_targets in branch_plan_calls] == ["context-llil"]
    branch_iter_items.clear()
    branch_plan_results.clear()


def test_branch_resolver_never_reads_stale_function_llil():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = {0x2000}
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x2000)]
    ctx = FakeContext()
    ctx.function.indirect_branches = [types.SimpleNamespace(source_addr=0x1000)]
    ctx.function.low_level_il = "function-llil"
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = "context-llil"
    branch_plan_results.clear()
    branch_plan_results["context-llil"] = [{"source": 0x2000, "targets": (0x3000,)}]

    workflow.resolve_jumps_llil(ctx)

    assert [llil for llil, _known_targets in branch_plan_calls] == ["context-llil"]
    branch_iter_items.clear()
    branch_plan_results.clear()


def test_branch_resolver_schedules_tag_cleanup_once_while_pending():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.stable = True
    FakeWorkflowState.updates = {}
    clear_tag_calls.clear()
    events = []
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
        add_analysis_completion_event=events.append,
        get_function_at=lambda start: ctx.function if start == ctx.function.start else None,
    )

    workflow.resolve_jumps_llil(ctx)
    workflow.resolve_jumps_llil(ctx)

    assert len(events) == 1
    assert "dispatchthis_gadget_map" not in ctx.view.session_data
    assert ctx.view.session_data["dispatchthis_tag_cleanup_pending"] == {ctx.function.start}
    events[0]()
    assert clear_tag_calls[-1] is ctx.function
    assert ctx.view.session_data["dispatchthis_tag_cleanup_pending"] == set()
    FakeWorkflowState.stable = False


def test_call_phase_never_runs_the_disabled_branch_pass_implicitly():
    submitted = []
    FakeWorkflowState.receipts = {0x1000: (0x2000,)}
    FakeWorkflowState.unmapped = {0x3000}
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {0x3000: (0x4000, 0x5000)}
    FakeWorkflowState.applied = []
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_plan_results["context-llil"] = [{"source": 0x3000, "targets": (0x4000, 0x5000)}]
    ctx = FakeContext()
    ctx.function.low_level_il = "function-llil"
    ctx.llil = "context-llil"
    ctx.function.set_user_indirect_branches = lambda source, targets: submitted.append((source, targets))

    workflow.resolve_calls_mlil(ctx)

    assert branch_plan_calls == []
    assert submitted == []
    assert FakeWorkflowState.applied == []
    assert "dispatchthis_llil_stable" not in ctx.view.session_data
    branch_plan_results.clear()
    FakeWorkflowState.updates = {}
    FakeWorkflowState.unmapped = set()


def test_call_phase_leaves_an_unstable_branch_phase_untouched():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x3000)]
    ctx = FakeContext()
    ctx.function.low_level_il = "function-llil"
    ctx.llil = "context-llil"
    ctx.view.add_analysis_completion_event = lambda _callback: None

    workflow.resolve_calls_mlil(ctx)

    assert branch_plan_calls == []
    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


def test_call_resolver_uses_active_provider_without_workflow_state():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_targets = []
    FakeWorkflowState.call_adjustment_checks = []
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    FakeWorkflowState.call_stable_marked = False
    FakeWorkflowState.call_cleanup_marked = False
    active_profile_calls.clear()
    call_plan_calls.clear()
    call_rewrite_calls.clear()
    ctx = FakeContext()
    callee_type = _function_type(1)
    ctx.view.get_function_at = lambda target: (
        types.SimpleNamespace(type=callee_type, name="sub_5000")
        if target == 0x5000
        else None
    )
    plan = {
        "call_il": types.SimpleNamespace(address=0x4000, params=[_typed_parameter()]),
        "call_addr": 0x4000,
        "target": 0x5000,
        "cleanup_roots": {7},
    }
    call_plan_results[:] = [plan]

    workflow.resolve_calls_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert call_plan_calls == [(ctx.view, ctx.mlil)]
    assert call_rewrite_calls == [(
        ctx,
        ctx.mlil,
        [{**plan, "decode_def": None, "cleanup_proven": True}],
    )]
    assert FakeWorkflowState.call_targets == [(0x4000, 0x5000)]
    assert len(FakeWorkflowState.call_adjustment_checks) == 1
    assert len(FakeWorkflowState.call_adjustment_checks[0][1].parameters) == 1
    assert FakeWorkflowState.call_receipts == {0x4000: 0x5000}
    assert FakeWorkflowState.call_stable_marked is True
    assert FakeWorkflowState.call_cleanup_marked is True
    FakeWorkflowState.stable = False
    call_plan_results.clear()


def test_call_adjustment_uses_call_site_parameters_over_narrow_callee():
    callback_type = FunctionType.create(ret=Type.int(8))
    call = types.SimpleNamespace(params=[
        _typed_parameter(),
        _typed_parameter(callback_type),
        _typed_parameter(Type.pointer_of_width(8, Type.void())),
    ])

    safe, adjusted = workflow._call_adjustment_type(
        None,
        call,
        types.SimpleNamespace(type=_function_type(0)),
    )

    assert safe
    assert [parameter.type for parameter in adjusted.parameters] == [
        parameter.expr_type for parameter in call.params
    ]


def test_call_phase_leaves_auto_type_alone_when_no_safe_override(monkeypatch):
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.call_targets = []
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    FakeWorkflowState.call_stable_marked = False
    call_plan_results[:] = [{
        "call_il": types.SimpleNamespace(
            address=0x4000,
            params=[types.SimpleNamespace(expr_type=None)],
        ),
        "call_addr": 0x4000,
        "target": 0x5000,
        "cleanup_roots": set(),
    }]
    ctx = FakeContext()
    ctx.view.get_function_at = lambda _target: types.SimpleNamespace(type=_function_type(1))
    mutations = []
    ctx.function.set_call_type_adjustment = lambda *args: mutations.append(args)
    monkeypatch.setattr(
        FakeWorkflowState,
        "call_adjustment_needed",
        lambda _self, _call_addr, _adjust_type: True,
    )

    workflow.resolve_calls_mlil(ctx)

    assert mutations == []
    assert FakeWorkflowState.call_receipts == {0x4000: 0x5000}
    assert FakeWorkflowState.call_stable_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.call_cleanup = True
    call_plan_results.clear()


def test_call_phase_in_place_rewrite_does_not_install_an_mlil_copy():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_targets = []
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    FakeWorkflowState.call_stable_marked = False
    call_plan_results[:] = [{
        "call_il": types.SimpleNamespace(address=0x4000, params=[]),
        "call_addr": 0x4000,
        "target": 0x5000,
        "cleanup_roots": set(),
    }]
    ctx = FakeContext()
    ctx.set_mlil_function = lambda _mlil: (_ for _ in ()).throw(RuntimeError("no commit"))

    workflow.resolve_calls_mlil(ctx)

    assert FakeWorkflowState.call_targets == [(0x4000, 0x5000)]
    assert FakeWorkflowState.call_receipts == {0x4000: 0x5000}
    assert FakeWorkflowState.call_stable_marked is True
    assert ctx.committed is False
    call_plan_results.clear()
    FakeWorkflowState.stable = False


def test_call_phase_uses_call_site_type_for_narrow_callee():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_targets = []
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    FakeWorkflowState.call_stable_marked = False
    receipt_plan_parameters[0x4000] = [_typed_parameter()]
    call_plan_results[:] = [{
        "call_il": types.SimpleNamespace(address=0x4000, params=[_typed_parameter()]),
        "call_addr": 0x4000,
        "target": 0x5000,
        "cleanup_roots": set(),
    }]
    ctx = FakeContext()
    ctx.view.get_function_at = lambda _target: types.SimpleNamespace(type=_function_type(0))

    workflow.resolve_calls_mlil(ctx)

    assert FakeWorkflowState.call_targets == [(0x4000, 0x5000)]
    assert FakeWorkflowState.call_target_receipts == {0x4000: 0x5000}
    assert FakeWorkflowState.call_receipts == {0x4000: 0x5000}
    assert FakeWorkflowState.call_stable_marked is True
    call_plan_results.clear()

    workflow.resolve_calls_mlil(ctx)

    assert FakeWorkflowState.call_receipts == {0x4000: 0x5000}
    assert FakeWorkflowState.call_stable_marked is True
    FakeWorkflowState.call_target_receipts = {}
    receipt_plan_parameters.clear()
    FakeWorkflowState.stable = False


def test_call_phase_rejects_unbound_old_receipts(monkeypatch):
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_receipts = {0x4000: 0x5000}
    FakeWorkflowState.call_target_receipts = {0x4000: 0x5000}
    FakeWorkflowState.call_stable_marked = False
    call_plan_results.clear()
    monkeypatch.setattr(workflow, "current_call_receipt_plans", lambda *_args: None)

    workflow.resolve_calls_mlil(FakeContext())

    assert FakeWorkflowState.call_stable_marked is False
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    FakeWorkflowState.stable = False


def test_call_provider_empty_batch_does_not_fallback_to_legacy_resolver():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    active_profile_calls.clear()
    call_plan_calls.clear()
    call_rewrite_calls.clear()
    call_plan_results.clear()
    ctx = FakeContext()

    workflow.resolve_calls_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert call_plan_calls == [(ctx.view, ctx.mlil)]
    assert call_rewrite_calls == [(ctx, ctx.mlil, [])]
    FakeWorkflowState.stable = False


def test_call_cleanup_respects_one_shot_receipt():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    FakeWorkflowState.call_cleanup_marked = False
    cleanup_decode_calls.clear()
    call_plan_results.clear()
    ctx = FakeContext()

    workflow.resolve_calls_mlil(ctx)

    assert cleanup_decode_calls == []
    assert FakeWorkflowState.call_cleanup_marked is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.call_cleanup = True


def test_call_cleanup_does_not_infer_ownership_from_a_call_receipt_location():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.call_cleanup_marked = False
    FakeWorkflowState.call_receipts = {0x8FB744: 0x8E04F8}
    FakeWorkflowState.call_target_receipts = {}
    cleanup_decode_calls.clear()
    call_plan_results.clear()
    ctx = FakeContext()
    callee_type = _function_type(0)
    ctx.view.get_function_at = lambda _target: types.SimpleNamespace(type=callee_type)

    workflow.resolve_calls_mlil(ctx)

    assert cleanup_decode_calls == []
    assert ctx.committed is False
    assert FakeWorkflowState.call_cleanup is True
    assert FakeWorkflowState.call_cleanup_marked is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.call_receipts = {}


def test_call_cleanup_keeps_its_receipt_open_after_current_mlil_nops():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.call_cleanup_marked = False
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [1]
    ctx = FakeContext()
    callee_type = _function_type(0)
    ctx.view.get_function_at = lambda _target: types.SimpleNamespace(type=callee_type)
    plan = {
        "call_il": types.SimpleNamespace(address=0x8FB744, params=[]),
        "call_addr": 0x8FB744,
        "target": 0x8E04F8,
        "cleanup_roots": {21621},
    }
    call_plan_results[:] = [plan]

    workflow.resolve_calls_mlil(ctx)

    assert cleanup_decode_calls == [((ctx.mlil, {21621}, "call"), {})]
    assert ctx.committed is False
    assert FakeWorkflowState.call_cleanup is True
    assert FakeWorkflowState.call_cleanup_marked is False
    assert FakeWorkflowState.call_stable_marked is True
    FakeWorkflowState.stable = False
    call_plan_results.clear()
    cleanup_decode_results.clear()


def test_call_cleanup_stays_open_when_the_current_ssa_slice_is_unproven():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.call_cleanup_marked = False
    FakeWorkflowState.call_receipts = {}
    FakeWorkflowState.call_target_receipts = {}
    cleanup_decode_calls.clear()
    ctx = FakeContext()
    callee_type = _function_type(0)
    ctx.view.get_function_at = lambda _target: types.SimpleNamespace(type=callee_type)
    call_plan_results[:] = [{
        "call_il": types.SimpleNamespace(address=0x8FB744, params=[]),
        "call_addr": 0x8FB744,
        "target": 0x8E04F8,
        "cleanup_proven": False,
    }]

    workflow.resolve_calls_mlil(ctx)

    assert cleanup_decode_calls == []
    assert FakeWorkflowState.call_cleanup is True
    assert FakeWorkflowState.call_cleanup_marked is False
    assert FakeWorkflowState.call_stable_marked is True
    assert FakeWorkflowState.call_target_receipts == {0x8FB744: 0x8E04F8}
    FakeWorkflowState.stable = False
    call_plan_results.clear()


def test_branch_translation_waits_for_global_stability(monkeypatch):
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = False
    translated = []
    monkeypatch.setattr(
        workflow,
        "translate_indirect_branch_conditions",
        lambda *_args: (translated.append(True), (None, 0, set()))[1],
    )

    workflow.translate_branches_mlil(FakeContext())

    assert translated == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False


def test_branch_cleanup_respects_one_shot_receipt():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = False
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.branch_cleanup_marked = False
    cleanup_decode_calls.clear()
    ctx = FakeContext()

    workflow.translate_branches_mlil(ctx)

    assert cleanup_decode_calls == []
    assert FakeWorkflowState.branch_cleanup_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.branch_cleanup = True


def test_branch_cleanup_keeps_its_receipt_open_after_current_mlil_nops():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = False
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [1]
    old_translate = workflow.translate_indirect_branch_conditions
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, mlil, _receipts: _condition_batch(
        mlil,
        sources={0x8DB6F8},
        roots={44},
    )
    ctx = FakeContext()

    try:
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate

    assert cleanup_decode_calls == [((ctx.mlil, {44}, "branch"), {})]
    assert ctx.committed is True
    assert FakeWorkflowState.branch_cleanup is True
    assert FakeWorkflowState.branch_cleanup_marked is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_branch_cleanup_merges_translation_and_current_receipt_roots():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [0]
    set_roots_before_results[:] = [{21621}]
    translated = []
    old_translate = workflow.translate_indirect_branch_conditions
    workflow.translate_indirect_branch_conditions = lambda ctx_arg, llil, mlil, receipts: (
        translated.append((ctx_arg, llil, mlil, receipts)),
        _condition_batch(mlil, sources={0x8DB6F8}, roots={44}),
    )[1]
    ctx = FakeContext()

    try:
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate

    assert translated == [(ctx, ctx.llil, ctx.mlil, ())]
    assert cleanup_decode_calls == [((ctx.mlil, {44}, "branch"), {})]
    assert FakeWorkflowState.branch_cleanup_marked is True
    assert ctx.committed is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_commit_mlil_reports_result_without_assignment_fallback():
    class Context:
        function = types.SimpleNamespace(name="sub_1000")

        def __init__(self, error=None):
            self.error = error
            self.set_calls = []
            self.assignment_attempted = False

        @property
        def mlil(self):
            return None

        @mlil.setter
        def mlil(self, _value):
            self.assignment_attempted = True

        def set_mlil_function(self, mlil):
            self.set_calls.append(mlil)
            if self.error is not None:
                raise self.error

    replacement = object()
    success = Context()
    failure = Context(RuntimeError("install failed"))

    assert workflow._commit_mlil(success, replacement) is True
    assert workflow._commit_mlil(failure, replacement) is False
    assert success.set_calls == [replacement]
    assert failure.set_calls == [replacement]
    assert success.assignment_attempted is False
    assert failure.assignment_attempted is False


def test_branch_cleanup_retries_when_replacement_install_fails():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [0]
    set_roots_before_results[:] = [{21621}]
    old_translate = workflow.translate_indirect_branch_conditions
    old_commit = workflow._commit_mlil
    ctx = FakeContext()
    translated_mlil = object()
    commits = []
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, _mlil, _receipts: _condition_batch(
        translated_mlil,
        sources={0x8DB6F8},
        roots={44},
    )
    install_results = [False, True]
    def commit_translated_mlil(ctx_arg, mlil_arg):
        commits.append((ctx_arg, mlil_arg))
        installed = install_results.pop(0)
        if installed:
            ctx_arg._mlil = mlil_arg
        return installed

    workflow._commit_mlil = commit_translated_mlil

    try:
        workflow.translate_branches_mlil(ctx)
        assert FakeWorkflowState.branch_cleanup is True
        assert FakeWorkflowState.branch_cleanup_marked is False
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate
        workflow._commit_mlil = old_commit

    assert cleanup_decode_calls == [((translated_mlil, {44}, "branch"), {})]
    assert commits == [(ctx, translated_mlil), (ctx, translated_mlil)]
    assert FakeWorkflowState.branch_cleanup is False
    assert FakeWorkflowState.branch_cleanup_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_branch_cleanup_retries_when_translation_is_rejected():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [0]
    set_roots_before_results[:] = [{21621}]
    old_translate = workflow.translate_indirect_branch_conditions
    old_commit = workflow._commit_mlil
    ctx = FakeContext()
    translated_mlil = object()
    translation_results = [
        _condition_batch(ctx.mlil, backend_failed=True),
        _condition_batch(translated_mlil, sources={0x8DB6F8}, roots={44}),
    ]
    commits = []
    workflow.translate_indirect_branch_conditions = lambda *_args: translation_results.pop(0)
    def commit_translated_mlil(ctx_arg, mlil_arg):
        commits.append((ctx_arg, mlil_arg))
        ctx_arg._mlil = mlil_arg
        return True

    workflow._commit_mlil = commit_translated_mlil

    try:
        workflow.translate_branches_mlil(ctx)
        assert cleanup_decode_calls == []
        assert commits == []
        assert FakeWorkflowState.branch_cleanup is True
        assert FakeWorkflowState.branch_cleanup_marked is False
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate
        workflow._commit_mlil = old_commit

    assert cleanup_decode_calls == [((translated_mlil, {44}, "branch"), {})]
    assert commits == [(ctx, translated_mlil)]
    assert FakeWorkflowState.branch_cleanup is False
    assert FakeWorkflowState.branch_cleanup_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_branch_cleanup_confirms_no_change_without_mlil_install():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [0]
    set_roots_before_results[:] = [{21621}]
    old_translate = workflow.translate_indirect_branch_conditions
    old_commit = workflow._commit_mlil
    ctx = FakeContext()
    commits = []
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, mlil, _receipts: _condition_batch(mlil)
    workflow._commit_mlil = lambda *_args: commits.append(True)

    try:
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate
        workflow._commit_mlil = old_commit

    assert cleanup_decode_calls == []
    assert commits == []
    assert FakeWorkflowState.branch_cleanup is False
    assert FakeWorkflowState.branch_cleanup_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.branch_cleanup = True
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_branch_translation_commits_current_mlil_before_local_cleanup_replanning():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    cleanup_decode_calls.clear()
    set_roots_before_results[:] = [{21621}]
    old_translate = workflow.translate_indirect_branch_conditions
    old_commit = workflow._commit_mlil
    ctx = FakeContext()
    translated_mlil = object()
    committed = []
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, _mlil, _receipts: _condition_batch(
        translated_mlil,
        sources={0x8DB6F8},
        roots={44},
    )
    def commit_current_mlil(ctx_arg, mlil_arg):
        committed.append((ctx_arg, mlil_arg))
        ctx_arg._mlil = mlil_arg
        return True

    def settle_current_mlil(mlil_arg, roots, phase_name, **kwargs):
        assert mlil_arg is translated_mlil
        cleanup_decode_calls.append(((mlil_arg, roots, phase_name), kwargs))
        return 1, True

    workflow._commit_mlil = commit_current_mlil
    old_settle_cleanup = workflow.settle_cleanup_decode
    workflow.settle_cleanup_decode = settle_current_mlil

    try:
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate
        workflow._commit_mlil = old_commit
        workflow.settle_cleanup_decode = old_settle_cleanup

    assert cleanup_decode_calls == [((translated_mlil, {44}, "branch"), {})]
    assert committed == [(ctx, translated_mlil)]
    assert FakeWorkflowState.branch_cleanup is True
    assert FakeWorkflowState.branch_cleanup_marked is False
    assert FakeWorkflowState.branch_cleanup_overlay is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.branch_cleanup_overlay = False
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_branch_translation_defers_deflatten_to_downstream_activity():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.branch_cleanup_marked = False
    FakeWorkflowState.receipts = {0x8DB6F8: (0x8DB6FC, 0x8DB700)}
    calls.clear()
    cleanup_decode_calls.clear()
    set_roots_before_results[:] = [{21621}]
    deflatten_rewrite_results.clear()
    old_translate = workflow.translate_indirect_branch_conditions
    old_commit = workflow._commit_mlil
    old_enabled = workflow._deflatten_enabled
    ctx = FakeContext()
    translated_mlil = object()
    deflattened_mlil = object()
    commits = []
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, _mlil, _receipts: _condition_batch(
        translated_mlil,
        sources={0x8DB6F8},
        roots={44},
    )
    deflatten_rewrite_results[:] = [(deflattened_mlil, 1)]

    def commit_current_mlil(ctx_arg, mlil_arg):
        commits.append((ctx_arg, mlil_arg))
        ctx_arg._mlil = mlil_arg
        return True

    workflow._commit_mlil = commit_current_mlil
    workflow._deflatten_enabled = lambda _func: True

    try:
        workflow.translate_branches_mlil(ctx)
        assert calls == []
        workflow.deflatten_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate
        workflow._commit_mlil = old_commit
        workflow._deflatten_enabled = old_enabled

    assert cleanup_decode_calls == [((translated_mlil, {44}, "branch"), {})]
    assert commits == [(ctx, translated_mlil), (ctx, deflattened_mlil)]
    assert calls[0] == ("compute", ctx.function.start, translated_mlil)
    assert calls[1][0] == "rewrite"
    assert ctx.view.session_data["dispatchthis_mlil_stable"][ctx.function.start] is True
    assert FakeWorkflowState.branch_cleanup is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.branch_cleanup = True
    FakeWorkflowState.branch_cleanup_overlay = False
    FakeWorkflowState.call_cleanup = True
    deflatten_rewrite_results.clear()
    set_roots_before_results.clear()


def test_mixed_condition_outcomes_install_ready_site_but_block_and_dedupe_deflatten(monkeypatch):
    ready_source = 0x8DB6F8
    failed_source = 0x8DB700

    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.conditions = {
        ready_source: _condition_receipt_data(ready_source),
        failed_source: _condition_receipt_data(failed_source),
    }
    FakeWorkflowState.condition_failures_data = {}
    FakeWorkflowState.conditions_complete_flag = True
    FakeWorkflowState.branch_cleanup = True
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [0]
    calls.clear()
    warnings = []
    ctx = FakeContext()
    failure = branch_conditions.ConditionTranslationFailure(
        failed_source,
        branch_conditions.ConditionFailureReason.MLIL_MAPPING_MISSING,
        "test-only missing mapping",
    )
    results = (
        branch_conditions.ConditionTranslationResult(
            ready_source,
            branch_conditions.ConditionTranslationStatus.REWRITE_READY,
        ),
        branch_conditions.ConditionTranslationResult(
            failed_source,
            branch_conditions.ConditionTranslationStatus.FAILED,
            failure,
        ),
    )
    old_translate = workflow.translate_indirect_branch_conditions
    monkeypatch.setattr(workflow, "log_warn", warnings.append)
    workflow.translate_indirect_branch_conditions = lambda _ctx, _llil, mlil, receipts: (
        _condition_batch(mlil, results=results, sources={ready_source}, roots={44})
        if {item.source for item in receipts} == {ready_source, failed_source}
        else (_ for _ in ()).throw(AssertionError("workflow lost current condition receipts"))
    )

    try:
        workflow.translate_branches_mlil(ctx)
        workflow.translate_branches_mlil(ctx)
        workflow.deflatten_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate

    assert ctx.committed is True
    assert cleanup_decode_calls == [
        ((ctx.mlil, {44}, "branch"), {}),
        ((ctx.mlil, {44}, "branch"), {}),
    ]
    assert FakeWorkflowState.condition_failures_data == {
        failed_source: "mlil_mapping_missing",
    }
    assert FakeWorkflowState.branch_cleanup is True
    assert len(warnings) == 1
    assert calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.conditions = {}
    FakeWorkflowState.condition_failures_data = {}
    cleanup_decode_results.clear()


def test_shared_transform_failure_still_diagnoses_an_unrelated_failed_site(monkeypatch):
    copy_source = 0x8DB6F8
    failed_source = 0x8DB700
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.conditions = {
        copy_source: _condition_receipt_data(copy_source),
        failed_source: _condition_receipt_data(failed_source),
    }
    FakeWorkflowState.condition_failures_data = {}
    FakeWorkflowState.conditions_complete_flag = True
    ctx = FakeContext()
    tags = []
    warnings = []
    errors = []
    mapping_failure = branch_conditions.ConditionTranslationFailure(
        failed_source,
        branch_conditions.ConditionFailureReason.MLIL_MAPPING_MISSING,
        "test-only missing mapping",
    )
    copy_failure = branch_conditions.ConditionTranslationFailure(
        copy_source,
        branch_conditions.ConditionFailureReason.COPY_FAILED,
        "test-only shared copy failure",
    )
    batch = _condition_batch(
        ctx.mlil,
        results=(
            branch_conditions.ConditionTranslationResult(
                failed_source,
                branch_conditions.ConditionTranslationStatus.FAILED,
                mapping_failure,
            ),
            branch_conditions.ConditionTranslationResult(
                copy_source,
                branch_conditions.ConditionTranslationStatus.FAILED,
                copy_failure,
            ),
        ),
        backend_failed=True,
    )
    old_translate = workflow.translate_indirect_branch_conditions
    monkeypatch.setattr(workflow, "translate_indirect_branch_conditions", lambda *_args: batch)
    monkeypatch.setattr(workflow, "publish_condition_failure_tag", lambda _bv, _func, failure: tags.append(failure))
    monkeypatch.setattr(workflow, "log_warn", warnings.append)
    monkeypatch.setattr(workflow, "log_error", errors.append)

    try:
        workflow.translate_branches_mlil(ctx)
    finally:
        workflow.translate_indirect_branch_conditions = old_translate

    assert tags == [mapping_failure]
    assert len(warnings) == 1
    assert "mlil_mapping_missing" in warnings[0]
    assert errors == [f"[workflow] {ctx.function.name}: branch-condition transform failed"]
    assert FakeWorkflowState.condition_failures_data == {
        failed_source: "mlil_mapping_missing",
        copy_source: "copy_failed",
    }
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.conditions = {}
    FakeWorkflowState.condition_failures_data = {}


def test_global_resolver_uses_active_provider_without_view_receipt():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.global_receipts = {}
    FakeWorkflowState.global_slots = []
    FakeWorkflowState.global_stable_marked = False
    FakeWorkflowState.globals_stable = False
    active_profile_calls.clear()
    global_plan_calls.clear()
    ctx = FakeContext()
    global_plan_results[:] = [{
        "slot_addr": 0xA43D70,
        "type": "uint64_t",
    }]

    workflow.resolve_globals_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert global_plan_calls == [(ctx.view, ctx.mlil)]
    assert ctx.typed_globals == [(0xA43D70, "uint64_t")]
    assert "dispatchthis_global_constant_slots" not in ctx.view.session_data
    assert FakeWorkflowState.global_slots == [(0xA43D70, "uint64_t")]
    assert FakeWorkflowState.global_stable_marked is False

    ctx.typed_globals.clear()
    FakeWorkflowState.global_slots.clear()
    workflow.resolve_globals_mlil(ctx)

    assert ctx.typed_globals == []
    assert FakeWorkflowState.global_slots == []
    assert FakeWorkflowState.global_stable_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    global_plan_results.clear()


def test_global_resolver_ignores_stale_view_receipt():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.global_receipts = {}
    FakeWorkflowState.global_slots = []
    FakeWorkflowState.global_stable_marked = False
    FakeWorkflowState.globals_stable = False
    active_profile_calls.clear()
    global_plan_calls.clear()
    ctx = FakeContext()
    stale_receipt = {0xDEAD: "stale"}
    ctx.view.session_data["dispatchthis_global_constant_slots"] = stale_receipt.copy()
    global_plan_results[:] = [
        {
            "slot_addr": 0x11F57B8,
            "type": "void const* const",
        },
    ]

    workflow.resolve_globals_mlil(ctx)

    assert ctx.typed_globals == [
        (0x11F57B8, "void const* const"),
    ]
    assert ctx.view.session_data["dispatchthis_global_constant_slots"] == stale_receipt
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    global_plan_results.clear()


def test_global_resolver_rejects_conflicting_types_for_one_slot_atomically():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.global_receipts = {}
    FakeWorkflowState.global_slots = []
    FakeWorkflowState.global_stable_marked = False
    FakeWorkflowState.globals_stable = True
    ctx = FakeContext()
    global_plan_results[:] = [
        {"slot_addr": 0xA43D70, "type": "uint64_t"},
        {"slot_addr": 0xA43D70, "type": "void const* const"},
    ]

    workflow.resolve_globals_mlil(ctx)

    assert ctx.typed_globals == []
    assert FakeWorkflowState.global_slots == []
    assert FakeWorkflowState.global_stable_marked is False
    assert FakeWorkflowState.globals_stable is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    global_plan_results.clear()


def test_global_provider_empty_batch_does_not_fallback_to_legacy_resolver():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.global_receipts = {}
    FakeWorkflowState.global_stable_marked = False
    FakeWorkflowState.globals_stable = False
    active_profile_calls.clear()
    global_plan_calls.clear()
    global_plan_results.clear()
    ctx = FakeContext()

    workflow.resolve_globals_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert global_plan_calls == [(ctx.view, ctx.mlil)]
    assert ctx.typed_globals == []
    assert FakeWorkflowState.global_stable_marked is True
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False


def test_global_resolver_does_not_stabilize_when_receipts_no_longer_verify():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.global_receipts = {0xA43D70: "uint64_t"}
    FakeWorkflowState.global_stable_marked = False
    FakeWorkflowState.globals_stable = True
    active_profile_calls.clear()
    global_plan_calls.clear()
    global_plan_results.clear()
    ctx = FakeContext()

    workflow.resolve_globals_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert global_plan_calls == [(ctx.view, ctx.mlil)]
    assert FakeWorkflowState.global_stable_marked is False
    assert FakeWorkflowState.globals_stable is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.global_receipts = {}


def test_recover_phi_stores_uses_external_plan_after_global_stability():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    active_profile_calls.clear()
    correlated_plan_calls.clear()
    correlated_plan_results[:] = [semantics.CorrelatedStorePlan(
        store_il=object(),
        join_block=object(),
        size=4,
        arms=(
            semantics.CorrelatedStoreArm(
                predecessor=object(),
                incoming_edge=object(),
                goto_il=object(),
                dest_expr=object(),
                dest_addr=0x1000,
                src_expr=object(),
                src_addr=0x2000,
            ),
            semantics.CorrelatedStoreArm(
                predecessor=object(),
                incoming_edge=object(),
                goto_il=object(),
                dest_expr=object(),
                dest_addr=0x1004,
                src_expr=object(),
                src_addr=0x2004,
            ),
        ),
    )]
    correlated_rewrite_calls.clear()
    rewritten_mlil = object()
    correlated_rewrite_results[:] = [(rewritten_mlil, 1)]
    ctx = FakeContext()

    workflow.recover_phi_stores_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert correlated_plan_calls == [(ctx.view, ctx.function, ctx.mlil)]
    assert correlated_rewrite_calls == [(ctx, ctx.mlil, tuple(correlated_plan_results))]
    assert ctx.installed_mlil is rewritten_mlil
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False
    correlated_plan_results.clear()
    correlated_rewrite_results.clear()


def test_string_decrypt_waits_for_branch_call_and_global_stability():
    ctx = FakeContext()
    string_decrypt_calls.clear()

    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    assert workflow.string_decrypt_mlil(ctx) == 0

    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = False
    assert workflow.string_decrypt_mlil(ctx) == 0

    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = False
    assert workflow.string_decrypt_mlil(ctx) == 0

    assert string_decrypt_calls == []
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False


def test_string_decrypt_does_not_require_deflatten_stability():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.cleanup_invalidated = False
    active_profile_calls.clear()
    string_decrypt_calls.clear()
    string_decrypt_results[:] = [[
        semantics.StringRecoveryFact(0x5000, 0x7000, 0x6000, b"first"),
        semantics.StringRecoveryFact(0x5010, 0x7001, 0x6001, b"second"),
    ]]
    ctx = FakeContext()

    assert workflow.string_decrypt_mlil(ctx) == 2
    assert active_profile_calls == [ctx.view]
    assert string_decrypt_calls == [(ctx.view, ctx.function, ctx.mlil, frozenset())]
    assert FakeWorkflowState.cleanup_invalidated is True
    assert "dispatchthis_mlil_stable" not in ctx.view.session_data
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False


def test_string_decrypt_leaves_cleanup_receipts_when_comments_are_unchanged():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    FakeWorkflowState.cleanup_invalidated = False
    active_profile_calls.clear()
    string_decrypt_calls.clear()
    string_decrypt_results[:] = [[]]
    ctx = FakeContext()

    assert workflow.string_decrypt_mlil(ctx) == 0
    assert active_profile_calls == [ctx.view]
    assert string_decrypt_calls == [(ctx.view, ctx.function, ctx.mlil, frozenset())]
    assert FakeWorkflowState.cleanup_invalidated is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False


def test_noreturn_type_detection_and_fallthrough_callsite():
    block = types.SimpleNamespace(start=0, end=2, outgoing_edges=[])
    first = types.SimpleNamespace(instr_index=10, il_basic_block=block)
    call = types.SimpleNamespace(instr_index=11, il_basic_block=block)
    mlil = [first, call]

    assert workflow._type_is_noreturn(types.SimpleNamespace(can_return=False))
    assert not workflow._type_is_noreturn(types.SimpleNamespace(can_return=True))
    assert workflow._call_has_fallthrough(mlil, first)
    assert not workflow._call_has_fallthrough(mlil, call)
