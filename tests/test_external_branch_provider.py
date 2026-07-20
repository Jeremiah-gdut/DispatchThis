import types

from conftest import load_plugin_module


class FakeBranch:
    def __init__(self, source_addr, dest_addr):
        self.source_addr = source_addr
        self.dest_addr = dest_addr
        self.auto_defined = False


class FakeFunction:
    name = "sub_1000"
    start = 0x1000

    def __init__(self):
        self.session_data = {}
        self.indirect_branches = []
        self.unresolved_indirect_branches = []
        self.submitted = []

    def set_user_indirect_branches(self, source, targets):
        self.submitted.append((source, tuple(targets)))
        self.indirect_branches = [FakeBranch(source, target) for _arch, target in targets]


class FakeView:
    def __init__(self, func):
        self.arch = types.SimpleNamespace(name="aarch64")
        self.session_data = {}
        self.function = func

    def get_function_at(self, start):
        return self.function if start == self.function.start else None


def _context(func, bv, llil):
    return types.SimpleNamespace(function=func, view=bv, llil=llil)


def _jump(address=0x1000):
    return types.SimpleNamespace(
        address=address,
        dest=types.SimpleNamespace(expr_index=7),
        operation="jump",
        instr_index=3,
        expr_index=3,
    )


def test_external_branch_provider_submits_once_then_converges(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    received_queries = []

    def branch_targets(query):
        received_queries.append(query)
        return semantics.CompleteBatch((semantics.BranchTargetFact(jump, (0x2000,)),))

    provider = semantics.SampleSemantics(
        provider_id="branch-provider",
        name="Branch provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=branch_targets,
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    jumps = [jump]
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: tuple(jumps))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)
    monkeypatch.setattr(workflow, "clear_resolved_indirect_branch_tags", lambda _func: None)
    monkeypatch.setattr(workflow, "_schedule_tag_cleanup", lambda *_args: None)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert received_queries == [semantics.BranchTargetQuery(bv, func, llil)]
    assert func.submitted == [(0x1000, ((bv.arch, 0x2000),))]
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["conditions"] == {}

    jumps.clear()
    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == [(0x1000, ((bv.arch, 0x2000),))]
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["stable"] is True


def test_conditional_fact_is_captured_before_branch_metadata_mutation(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    branch_conditions = load_plugin_module("plugins.DispatchThis.passes.medium.branch_translate")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    condition = types.SimpleNamespace()
    receipt = branch_conditions.ConditionReceipt(
        0x1000,
        branch_conditions.ILAnchor(0x900, 1, (("dest", -1),), "LLIL_CMP_NE", 1),
        0x3000,
        0x2000,
    )
    provider = semantics.SampleSemantics(
        provider_id="conditional-provider",
        name="Conditional provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch(
            (
                semantics.BranchTargetFact(
                    jump,
                    (0x2000, 0x3000),
                    condition=condition,
                    true_target=0x3000,
                    false_target=0x2000,
                ),
            )
        ),
    )
    captured = []

    def capture(current_llil, source, current_condition, true_target, false_target):
        captured.append((current_llil, source, current_condition, true_target, false_target, tuple(func.submitted)))
        return receipt

    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "capture_condition_receipt", capture, raising=False)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)
    monkeypatch.setattr(workflow, "clear_resolved_indirect_branch_tags", lambda _func: None)
    monkeypatch.setattr(workflow, "_schedule_tag_cleanup", lambda *_args: None)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert captured == [(llil, 0x1000, condition, 0x3000, 0x2000, ())]
    assert func.submitted == [(0x1000, ((bv.arch, 0x2000), (bv.arch, 0x3000)))]
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["conditions"] == {
        0x1000: receipt.as_data(),
    }


def test_missing_provider_binding_refuses_branch_work_without_a_fallback(monkeypatch):
    providers = load_plugin_module("plugins.DispatchThis.providers")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    ensured = []
    warnings = []
    monkeypatch.setattr(
        workflow,
        "active_provider",
        lambda _bv: (_ for _ in ()).throw(providers.ProviderBindingError("no selected provider")),
    )
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: ensured.append(True) or True)
    monkeypatch.setattr(workflow, "log_warn", warnings.append)

    workflow.resolve_jumps_llil(_context(func, bv, types.SimpleNamespace()))

    assert func.submitted == []
    assert func.session_data == {}
    assert ensured == []
    assert warnings == ["[workflow] sub_1000: provider binding unavailable: no selected provider"]


def test_branch_batch_outcomes_are_distinct(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    debug = []
    warnings = []
    monkeypatch.setattr(workflow, "log_debug", debug.append)
    monkeypatch.setattr(workflow, "log_warn", warnings.append)
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)

    missing = semantics.SampleSemantics(
        provider_id="missing-slot",
        name="Missing slot",
        api_version=semantics.CORE_API_VERSION,
    )
    assert workflow._provider_branch_plan(bv, func, llil, missing) is None
    assert debug == ["[workflow] sub_1000: provider does not implement branch target recovery"]

    inconclusive = semantics.SampleSemantics(
        provider_id="inconclusive-slot",
        name="Inconclusive slot",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.Inconclusive("missing definition"),
    )
    assert workflow._provider_branch_plan(bv, func, llil, inconclusive) is None
    assert warnings == ["[workflow] sub_1000: branch provider was inconclusive: missing definition"]

    empty = semantics.SampleSemantics(
        provider_id="empty-batch",
        name="Empty batch",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch(()),
    )
    assert workflow._provider_branch_plan(bv, func, llil, empty) == []

    complete = semantics.SampleSemantics(
        provider_id="complete-batch",
        name="Complete batch",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch((semantics.BranchTargetFact(jump, (0x2000,)),)),
    )
    assert workflow._provider_branch_plan(bv, func, llil, complete) == [
        {
            "source": 0x1000,
            "dest_expr_index": 7,
            "targets": (0x2000,),
            "jump_il": jump,
        }
    ]


def test_multitarget_branch_fact_submits_once_without_an_llil_rewrite(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    provider = semantics.SampleSemantics(
        provider_id="multi-target-provider",
        name="Multi target provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch(
            (semantics.BranchTargetFact(jump, (0x2000, 0x3000)),)
        ),
    )
    rewrites = []
    jumps = [jump]
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: tuple(jumps))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: rewrites.append(True))
    monkeypatch.setattr(workflow, "clear_resolved_indirect_branch_tags", lambda _func: None)
    monkeypatch.setattr(workflow, "_schedule_tag_cleanup", lambda *_args: None)

    workflow.resolve_jumps_llil(_context(func, bv, llil))
    jumps.clear()
    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == [
        (0x1000, ((bv.arch, 0x2000), (bv.arch, 0x3000))),
    ]
    assert rewrites == []
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["stable"] is True
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["conditions"] == {}


def test_inconclusive_or_invalid_external_branch_result_creates_no_receipt(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    func.indirect_branches = [FakeBranch(0x1000, 0x3000)]
    bv = FakeView(func)
    llil = types.SimpleNamespace()

    inconclusive_provider = semantics.SampleSemantics(
        provider_id="inconclusive-provider",
        name="Inconclusive provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.Inconclusive("missing definition"),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: inconclusive_provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (_jump(),))

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == []
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}

    invalid_provider = semantics.SampleSemantics(
        provider_id="invalid-provider",
        name="Invalid provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: {"source": 0x1000},
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: invalid_provider)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == []
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}


def test_matching_current_metadata_is_confirmed_only_by_a_current_provider_fact(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    func.indirect_branches = [FakeBranch(0x1000, 0x2000)]
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    provider = semantics.SampleSemantics(
        provider_id="confirm-current-metadata",
        name="Confirm current metadata",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch((semantics.BranchTargetFact(jump, (0x2000,)),)),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)
    monkeypatch.setattr(workflow, "clear_resolved_indirect_branch_tags", lambda _func: None)
    monkeypatch.setattr(workflow, "_schedule_tag_cleanup", lambda *_args: None)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == []
    branch = func.session_data["dispatchthis_workflow_state"]["branch"]
    assert branch["receipts"] == {0x1000: (0x2000,)}
    assert branch["stable"] is True


def test_empty_complete_batch_cannot_stabilize_unproven_current_metadata(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    func.indirect_branches = [FakeBranch(0x1000, 0x3000)]
    bv = FakeView(func)
    provider = semantics.SampleSemantics(
        provider_id="empty-with-old-metadata",
        name="Empty with old metadata",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch(()),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: ())
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)

    workflow.resolve_jumps_llil(_context(func, bv, types.SimpleNamespace()))

    branch = func.session_data["dispatchthis_workflow_state"]["branch"]
    assert func.submitted == []
    assert branch["receipts"] == {}
    assert branch["stable"] is False


def test_private_legacy_adapter_keeps_reopened_user_metadata_converged(monkeypatch):
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    func.indirect_branches = [FakeBranch(0x1000, 0x3000)]
    bv = FakeView(func)
    observed_receipts = []
    legacy = types.SimpleNamespace(
        resolve_branch_gadget=lambda _bv, _llil, receipts: observed_receipts.append(receipts) or [],
    )
    provider = types.SimpleNamespace(provider_id="legacy-profile")
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_legacy_profile", lambda _provider_id: legacy)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: ())
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "clear_resolved_indirect_branch_tags", lambda _func: None)
    monkeypatch.setattr(workflow, "_schedule_tag_cleanup", lambda *_args: None)

    workflow.resolve_jumps_llil(_context(func, bv, types.SimpleNamespace()))

    branch = func.session_data["dispatchthis_workflow_state"]["branch"]
    assert observed_receipts == [{0x1000: (0x3000,)}]
    assert func.submitted == []
    assert branch["receipts"] == {0x1000: (0x3000,)}
    assert branch["stable"] is True


def test_switching_to_a_legacy_adapter_requires_reproof_of_old_metadata(monkeypatch):
    ui = load_plugin_module("plugins.DispatchThis.ui")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    func.indirect_branches = [FakeBranch(0x1000, 0x3000)]
    bv = FakeView(func)
    monkeypatch.setattr(ui, "active_provider_id", lambda *_args: "external-a")
    monkeypatch.setattr(ui, "set_active_provider", lambda *_args: True)

    assert ui.use_provider(bv, func, "legacy-b", reanalyze=False)

    observed_receipts = []
    legacy = types.SimpleNamespace(
        resolve_branch_gadget=lambda _bv, _llil, receipts: observed_receipts.append(receipts) or [],
    )
    provider = types.SimpleNamespace(provider_id="legacy-b")
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_legacy_profile", lambda _provider_id: legacy)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: ())
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "_pending_reproof_functions", lambda _bv: frozenset({0x1000}))

    workflow.resolve_jumps_llil(_context(func, bv, types.SimpleNamespace()))

    branch = func.session_data["dispatchthis_workflow_state"]["branch"]
    assert observed_receipts == [{}]
    assert branch["receipts"] == {}
    assert branch["stable"] is False


def test_reproof_guard_clears_only_the_current_function(monkeypatch):
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    first = FakeFunction()
    first.start = 0x1000
    second = FakeFunction()
    second.start = 0x2000
    second.indirect_branches = [FakeBranch(0x2000, 0x3000)]
    bv = FakeView(first)
    bv.functions = [first, second]
    legacy = types.SimpleNamespace(resolve_branch_gadget=lambda *_args: [])
    provider = types.SimpleNamespace(provider_id="legacy-b")
    pending = {first.start, second.start}

    def set_pending(_bv, starts):
        pending.clear()
        pending.update(starts)
        return True

    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_legacy_profile", lambda _provider_id: legacy)
    monkeypatch.setattr(workflow, "_pending_reproof_functions", lambda _bv: frozenset(pending))
    monkeypatch.setattr(workflow, "_set_pending_reproof_functions", set_pending)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: ())
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "clear_resolved_indirect_branch_tags", lambda _func: None)
    monkeypatch.setattr(workflow, "_schedule_tag_cleanup", lambda *_args: None)

    workflow.resolve_jumps_llil(_context(first, bv, types.SimpleNamespace()))
    workflow.resolve_jumps_llil(_context(second, bv, types.SimpleNamespace()))

    branch = second.session_data["dispatchthis_workflow_state"]["branch"]
    assert pending == {second.start}
    assert branch["receipts"] == {}
    assert branch["stable"] is False


def test_rejected_external_batch_never_applies_a_partial_prefix(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    provider = semantics.SampleSemantics(
        provider_id="conflicting-provider",
        name="Conflicting provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch(
            (
                semantics.BranchTargetFact(jump, (0x2000,)),
                semantics.BranchTargetFact(jump, (0x3000,)),
            )
        ),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == []
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}


def test_stale_witness_and_provider_exception_leave_no_mutation_or_receipt(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    provider = semantics.SampleSemantics(
        provider_id="stale-provider",
        name="Stale provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch((semantics.BranchTargetFact(jump, (0x2000,)),)),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda *_args: [])

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == []
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}

    ensured = []
    exceptional = semantics.SampleSemantics(
        provider_id="exception-provider",
        name="Exception provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: (_ for _ in ()).throw(RuntimeError("broken provider")),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: exceptional)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: ensured.append(True) or True)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert ensured == []
    assert func.submitted == []
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}


def test_readback_must_match_before_branch_receipt_is_recorded(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")

    class NoReadbackFunction(FakeFunction):
        def set_user_indirect_branches(self, source, targets):
            self.submitted.append((source, tuple(targets)))

    func = NoReadbackFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    provider = semantics.SampleSemantics(
        provider_id="readback-provider",
        name="Readback provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch((semantics.BranchTargetFact(jump, (0x2000,)),)),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)

    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    assert func.submitted == [(0x1000, ((bv.arch, 0x2000),))]
    assert func.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}


def test_unverified_target_overwrite_cannot_close_the_branch_phase(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")

    class StaleReadbackFunction(FakeFunction):
        def set_user_indirect_branches(self, source, targets):
            self.submitted.append((source, tuple(targets)))

    func = StaleReadbackFunction()
    func.indirect_branches = [FakeBranch(0x1000, 0x3000)]
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    jump = _jump()
    provider = semantics.SampleSemantics(
        provider_id="stale-readback-provider",
        name="Stale readback provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch((semantics.BranchTargetFact(jump, (0x2000,)),)),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    state = func.session_data["dispatchthis_workflow_state"]["branch"]
    assert state["receipts"] == {}
    assert state["stable"] is False


def test_omitted_current_jump_remains_unstable_after_proven_fact_applies(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    func = FakeFunction()
    bv = FakeView(func)
    llil = types.SimpleNamespace()
    first = _jump(0x1000)
    second = _jump(0x1100)
    provider = semantics.SampleSemantics(
        provider_id="partial-provider",
        name="Partial provider",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=lambda _query: semantics.CompleteBatch((semantics.BranchTargetFact(first, (0x2000,)),)),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _bv: provider)
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _func: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (first, second))
    monkeypatch.setattr(workflow, "validate_current_branch_plans", lambda _bv, _llil, plans: plans)
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)

    workflow.resolve_jumps_llil(_context(func, bv, llil))

    state = func.session_data["dispatchthis_workflow_state"]
    assert func.submitted == [(0x1000, ((bv.arch, 0x2000),))]
    assert state["branch"]["receipts"] == {0x1000: (0x2000,)}
    assert state["branch"]["stable"] is False
