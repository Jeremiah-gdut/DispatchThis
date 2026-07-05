import types

from conftest import load_plugin_module, temporary_modules


calls = []
branch_plan_calls = []
branch_plan_results = {}
call_plan_calls = []
call_plan_results = []
call_rewrite_calls = []
active_profile_calls = []
branch_iter_items = []
clear_tag_calls = []
nop_state_write_results = []
nop_state_write_calls = []


def fake_compute(_bv, func, mlil=None):
    calls.append(("compute", func.start, mlil))
    return [{"kind": "uncond", "state_tokens": {(0x1234, 8)}, "state_vars": {"state"}}]


def fake_apply(mlil, plans):
    calls.append(("apply", mlil, plans))
    return 1


def fake_resolve_llil_jump_plan(_bv, llil, known_targets=None):
    branch_plan_calls.append((llil, known_targets))
    return branch_plan_results.get(llil, [])


def fake_resolve_call_gadget(bv, mlil):
    call_plan_calls.append((bv, mlil))
    return list(call_plan_results)


def fake_apply_indirect_call_rewrites(bv, mlil, plans):
    call_rewrite_calls.append((bv, mlil, plans))
    return 0


def fake_active_profile(bv):
    active_profile_calls.append(bv)
    return types.SimpleNamespace(
        resolve_branch_gadget=fake_resolve_llil_jump_plan,
        resolve_call_gadget=fake_resolve_call_gadget,
    )


class FakeWorkflowState:
    receipts = {}
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

    def __init__(self, _func):
        pass

    @staticmethod
    def unmapped_unresolved_sources(_func):
        return FakeWorkflowState.unmapped

    def branch_stable(self, _func):
        return self.stable

    def branch_targets(self):
        return self.receipts

    def branch_updates_for(self, _resolved_targets):
        return self.updates

    def mark_branch_stable(self):
        FakeWorkflowState.marked_stable = True

    def mark_branch_applied(self, source, targets):
        FakeWorkflowState.applied.append((source, targets))
        return False

    def mark_call_target(self, call_addr, target):
        FakeWorkflowState.call_targets.append((call_addr, target))
        FakeWorkflowState.call_target_receipts[call_addr] = target
        return False

    def call_adjustment_needed(self, call_addr, target):
        FakeWorkflowState.call_adjustment_checks.append((call_addr, target))
        return False

    def mark_call_stable(self):
        FakeWorkflowState.call_stable_marked = True

    def mark_call_cleanup_done(self):
        FakeWorkflowState.call_cleanup_marked = True


def forbidden_plan_indirect_calls(*_args, **_kwargs):
    raise AssertionError("workflow call planning must go through the active profile")


_FAKE_MODULES = {
    "plugins.DispatchThis.passes.medium.deflatten": types.SimpleNamespace(
        compute_redirections=fake_compute,
        apply_redirections_il=fake_apply,
    ),
    "plugins.DispatchThis.passes.medium.nop_pass": types.SimpleNamespace(
        nop_deflatten_state_writes=lambda *args, **kwargs: (
            nop_state_write_calls.append((args, kwargs)),
            nop_state_write_results.pop(0) if nop_state_write_results else 0,
        )[1],
    ),
    "plugins.DispatchThis.passes.medium.indirect_calls": types.SimpleNamespace(
        apply_indirect_call_rewrites=fake_apply_indirect_call_rewrites,
        plan_indirect_calls=forbidden_plan_indirect_calls,
    ),
    "plugins.DispatchThis.passes.medium.branch_conditions": types.SimpleNamespace(
        translate_indirect_branch_conditions=lambda *_args, **_kwargs: (None, 0, set()),
    ),
    "plugins.DispatchThis.passes.medium.phase_cleanup": types.SimpleNamespace(
        cleanup_decode=lambda *_args, **_kwargs: 0,
        set_roots_before=lambda *_args, **_kwargs: set(),
    ),
    "plugins.DispatchThis.passes.medium.global_constants": types.SimpleNamespace(
        CONST_SLOT_TYPE="uint64_t",
        plan_global_constant_slots=lambda *_args, **_kwargs: [],
    ),
    "plugins.DispatchThis.passes.low.gadget_llil": types.SimpleNamespace(
        apply_llil_jump_rewrites=lambda *_args, **_kwargs: 0,
        clear_resolved_indirect_branch_tags=lambda func: clear_tag_calls.append(func),
        iter_llil_indirect_jumps=lambda _llil: iter(branch_iter_items),
        resolve_llil_jump_plan=fake_resolve_llil_jump_plan,
    ),
    "plugins.DispatchThis.profiles": types.SimpleNamespace(
        active_profile=fake_active_profile,
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


class FakeContext:
    def __init__(self):
        self.function = types.SimpleNamespace(
            start=0x9556D8,
            name="sub_9556d8",
            set_user_indirect_branches=lambda *_args: None,
        )
        self.view = types.SimpleNamespace(
            arch=types.SimpleNamespace(name="aarch64"),
            session_data={"dispatchthis_llil_stable": {self.function.start: True}},
        )
        self._mlil = object()
        self.committed = False

    @property
    def mlil(self):
        return self._mlil

    @mlil.setter
    def mlil(self, value):
        self.committed = value is self._mlil

    def set_mlil_function(self, mlil):
        self.committed = mlil is self._mlil


def test_deflatten_workflow_runs_without_branch_mirror_state():
    ctx = FakeContext()

    workflow.deflatten_mlil(ctx)

    assert calls[0] == ("compute", ctx.function.start, ctx.mlil)
    assert calls[1][0] == "apply"
    assert ctx.committed is True
    assert ctx.view.session_data["dispatchthis_mlil_stable"][ctx.function.start] is True


def test_branch_resolver_reuses_branch_receipts_as_known_targets():
    FakeWorkflowState.receipts = {0x1000: (0x2000, 0x3000)}
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

    assert [known_targets for _llil, known_targets in branch_plan_calls] == [FakeWorkflowState.receipts]
    assert active_profile_calls == [ctx.view]
    assert "dispatchthis_gadget_map" not in ctx.view.session_data


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


def test_branch_resolver_uses_function_llil_fallback_for_newly_discovered_jump():
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
    branch_plan_results["function-llil"] = [{"source": 0x2000, "targets": (0x3000,)}]

    workflow.resolve_jumps_llil(ctx)

    assert [llil for llil, _known_targets in branch_plan_calls] == ["function-llil"]
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


def test_call_phase_submits_pending_function_llil_branches_before_call_work():
    submitted = []
    FakeWorkflowState.receipts = {0x1000: (0x2000,)}
    FakeWorkflowState.unmapped = {0x3000}
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {0x3000: (0x4000, 0x5000)}
    FakeWorkflowState.applied = []
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_plan_results["function-llil"] = [{"source": 0x3000, "targets": (0x4000, 0x5000)}]
    ctx = FakeContext()
    ctx.function.low_level_il = "function-llil"
    ctx.function.set_user_indirect_branches = lambda source, targets: submitted.append((source, targets))

    workflow.resolve_calls_mlil(ctx)

    assert [llil for llil, _known_targets in branch_plan_calls] == ["function-llil"]
    assert submitted == [(0x3000, [(ctx.view.arch, 0x4000), (ctx.view.arch, 0x5000)])]
    assert FakeWorkflowState.applied == [(0x3000, (0x4000, 0x5000))]
    assert ctx.view.session_data["dispatchthis_llil_stable"] == {}
    branch_plan_results.clear()
    FakeWorkflowState.updates = {}
    FakeWorkflowState.unmapped = set()


def test_call_resolver_uses_active_profile_without_workflow_state():
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
    plan = {
        "call_il": types.SimpleNamespace(address=0x4000),
        "call_addr": 0x4000,
        "target": 0x5000,
        "cleanup_roots": {7},
    }
    call_plan_results[:] = [plan]

    workflow.resolve_calls_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert call_plan_calls == [(ctx.view, ctx.mlil)]
    assert call_rewrite_calls == [(ctx.view, ctx.mlil, [plan])]
    assert FakeWorkflowState.call_targets == [(0x4000, 0x5000)]
    assert FakeWorkflowState.call_adjustment_checks == [(0x4000, 0x5000)]
    assert FakeWorkflowState.call_stable_marked is True
    assert FakeWorkflowState.call_cleanup_marked is True
    FakeWorkflowState.stable = False
    call_plan_results.clear()


def test_call_profile_hook_miss_does_not_fallback_to_default_resolver():
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
    assert call_rewrite_calls == [(ctx.view, ctx.mlil, [])]
    FakeWorkflowState.stable = False


def test_cleanup_waits_for_deflatten_stability():
    ctx = FakeContext()
    ctx.view.session_data["dispatchthis_mlil_stable"] = {}
    nop_state_write_calls.clear()

    workflow.cleanup_mlil(ctx)

    assert nop_state_write_calls == []
    assert ctx.committed is False


def test_cleanup_commits_when_deflatten_state_writes_are_nopped():
    ctx = FakeContext()
    ctx.view.session_data["dispatchthis_mlil_stable"] = {ctx.function.start: True}
    nop_state_write_calls.clear()
    nop_state_write_results[:] = [1]

    workflow.cleanup_mlil(ctx)

    assert nop_state_write_calls == [((ctx.view, ctx.function), {"mlil": ctx.mlil})]
    assert ctx.committed is True


def test_cleanup_does_not_commit_when_no_deflatten_state_writes_are_nopped():
    ctx = FakeContext()
    ctx.view.session_data["dispatchthis_mlil_stable"] = {ctx.function.start: True}
    nop_state_write_calls.clear()
    nop_state_write_results[:] = [0]

    workflow.cleanup_mlil(ctx)

    assert nop_state_write_calls == [((ctx.view, ctx.function), {"mlil": ctx.mlil})]
    assert ctx.committed is False


def test_noreturn_type_detection_and_fallthrough_callsite():
    block = types.SimpleNamespace(start=0, end=2, outgoing_edges=[])
    first = types.SimpleNamespace(instr_index=10, il_basic_block=block)
    call = types.SimpleNamespace(instr_index=11, il_basic_block=block)
    mlil = [first, call]

    assert workflow._type_is_noreturn("void() __noreturn")
    assert workflow._call_has_fallthrough(mlil, first)
    assert not workflow._call_has_fallthrough(mlil, call)

if __name__ == "__main__":
    test_deflatten_workflow_runs_without_branch_mirror_state()
    test_branch_resolver_reuses_branch_receipts_as_known_targets()
    test_branch_resolver_does_not_stabilize_unparsed_indirect_jumps()
    test_branch_resolver_does_not_stabilize_unparsed_later_jump_after_partial_mapping()
    test_branch_resolver_uses_function_llil_fallback_for_newly_discovered_jump()
    test_branch_resolver_schedules_tag_cleanup_once_while_pending()
    test_call_phase_submits_pending_function_llil_branches_before_call_work()
    test_call_resolver_uses_active_profile_without_workflow_state()
    test_call_profile_hook_miss_does_not_fallback_to_default_resolver()
    test_cleanup_waits_for_deflatten_stability()
    test_cleanup_commits_when_deflatten_state_writes_are_nopped()
    test_cleanup_does_not_commit_when_no_deflatten_state_writes_are_nopped()
    test_noreturn_type_detection_and_fallthrough_callsite()
