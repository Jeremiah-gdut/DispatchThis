import types

from conftest import load_plugin_module, temporary_modules


calls = []
branch_plan_calls = []
branch_plan_results = {}
call_plan_calls = []
call_plan_results = []
call_rewrite_calls = []
global_plan_calls = []
global_plan_results = []
active_profile_calls = []
branch_iter_items = []
clear_tag_calls = []
nop_state_write_results = []
nop_state_write_calls = []
cleanup_decode_calls = []
cleanup_decode_results = []
set_roots_before_calls = []
set_roots_before_results = []


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


def fake_plan_global_constant_slots(bv, mlil):
    global_plan_calls.append((bv, mlil))
    return list(global_plan_results)


def fake_active_profile(bv):
    active_profile_calls.append(bv)
    return types.SimpleNamespace(
        resolve_branch_gadget=fake_resolve_llil_jump_plan,
        resolve_call_gadget=fake_resolve_call_gadget,
        plan_global_constant_slots=fake_plan_global_constant_slots,
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
    branch_cleanup_marked = False
    global_receipts = {}
    global_slots = []
    global_stable_marked = False
    globals_stable = False
    calls_stable = False
    branch_cleanup = True
    call_cleanup = True

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

    def branch_cleanup_needed(self):
        return FakeWorkflowState.branch_cleanup

    def mark_branch_cleanup_done(self):
        FakeWorkflowState.branch_cleanup_marked = True

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


def forbidden_plan_indirect_calls(*_args, **_kwargs):
    raise AssertionError("workflow call planning must go through the active profile")


def forbidden_plan_global_constant_slots(*_args, **_kwargs):
    raise AssertionError("workflow global planning must go through the active profile")


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
        cleanup_decode=lambda *args, **kwargs: (
            cleanup_decode_calls.append((args, kwargs)),
            cleanup_decode_results.pop(0) if cleanup_decode_results else 0,
        )[1],
        set_roots_before=lambda *args, **kwargs: (
            set_roots_before_calls.append((args, kwargs)),
            set_roots_before_results.pop(0) if set_roots_before_results else set(),
        )[1],
    ),
    "plugins.DispatchThis.passes.medium.global_constants": types.SimpleNamespace(
        CONST_SLOT_TYPE="uint64_t",
        plan_global_constant_slots=forbidden_plan_global_constant_slots,
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
            session_data={},
        )
        self.typed_globals = []

        def parse_type_string(_decl):
            return ("uint64_t", None)

        def get_data_var_at(addr):
            return self.view.session_data.setdefault("data_vars", {}).get(addr)

        def define_user_data_var(addr, type_):
            self.typed_globals.append((addr, type_))
            self.view.session_data.setdefault("data_vars", {})[addr] = types.SimpleNamespace(type=type_)

        self.view.parse_type_string = parse_type_string
        self.view.get_data_var_at = get_data_var_at
        self.view.define_user_data_var = define_user_data_var
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
    FakeWorkflowState.stable = True
    FakeWorkflowState.globals_stable = True
    calls.clear()
    ctx = FakeContext()

    workflow.deflatten_mlil(ctx)

    assert calls[0] == ("compute", ctx.function.start, ctx.mlil)
    assert calls[1][0] == "apply"
    assert ctx.committed is True
    assert ctx.view.session_data["dispatchthis_mlil_stable"][ctx.function.start] is True
    assert "dispatchthis_llil_stable" not in ctx.view.session_data
    FakeWorkflowState.stable = False
    FakeWorkflowState.globals_stable = False


def test_deflatten_waits_for_global_phase_stability():
    FakeWorkflowState.stable = True
    FakeWorkflowState.globals_stable = False
    calls.clear()
    ctx = FakeContext()

    workflow.deflatten_mlil(ctx)

    assert calls == []
    FakeWorkflowState.stable = False


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


def test_branch_resolver_removes_target_user_functions_from_workflow_layer():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    FakeWorkflowState.updates = {}
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x2000)]
    ctx = FakeContext()
    ctx.llil = "context-llil"
    target_func = types.SimpleNamespace(start=0x3000)
    removed = []
    ctx.view.get_function_at = lambda target: target_func if target == 0x3000 else None
    ctx.view.remove_user_function = removed.append
    ctx.view.add_analysis_completion_event = lambda _callback: None
    branch_plan_results["context-llil"] = [{
        "source": 0x2000,
        "targets": (0x3000,),
        "dest_expr_index": 7,
    }]

    workflow.resolve_jumps_llil(ctx)

    assert removed == [target_func]
    branch_iter_items.clear()
    branch_plan_results.clear()


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


def test_branch_profile_hook_miss_does_not_retry_context_llil():
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
    assert "dispatchthis_llil_stable" not in ctx.view.session_data
    branch_plan_results.clear()
    FakeWorkflowState.updates = {}
    FakeWorkflowState.unmapped = set()


def test_call_phase_does_not_mark_branch_stable_when_pending_function_llil_has_uncovered_jump():
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
    ctx.view.add_analysis_completion_event = lambda _callback: None

    workflow.resolve_calls_mlil(ctx)

    assert [llil for llil, _known_targets in branch_plan_calls] == ["function-llil"]
    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


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


def test_call_cleanup_respects_one_shot_receipt():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = False
    FakeWorkflowState.call_cleanup_marked = False
    cleanup_decode_calls.clear()
    call_plan_results.clear()
    ctx = FakeContext()

    workflow.resolve_calls_mlil(ctx)

    assert cleanup_decode_calls == []
    assert FakeWorkflowState.call_cleanup_marked is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.call_cleanup = True


def test_call_cleanup_retries_after_current_mlil_was_changed():
    FakeWorkflowState.stable = True
    FakeWorkflowState.call_cleanup = True
    FakeWorkflowState.call_cleanup_marked = False
    cleanup_decode_calls.clear()
    cleanup_decode_results[:] = [1]
    set_roots_before_results[:] = [{21621}]
    ctx = FakeContext()
    plan = {
        "call_il": types.SimpleNamespace(address=0x8FB744),
        "call_addr": 0x8FB744,
        "target": 0x8E04F8,
        "cleanup_roots": set(),
    }
    call_plan_results[:] = [plan]

    workflow.resolve_calls_mlil(ctx)

    assert cleanup_decode_calls == [((ctx.mlil, {21621}, "call"), {})]
    assert ctx.committed is True
    assert FakeWorkflowState.call_cleanup_marked is False
    FakeWorkflowState.stable = False
    call_plan_results.clear()
    cleanup_decode_results.clear()
    set_roots_before_results.clear()


def test_branch_cleanup_respects_one_shot_receipt():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.branch_cleanup = False
    FakeWorkflowState.branch_cleanup_marked = False
    cleanup_decode_calls.clear()
    ctx = FakeContext()

    workflow.translate_branches_mlil(ctx)

    assert cleanup_decode_calls == []
    assert FakeWorkflowState.branch_cleanup_marked is False
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.branch_cleanup = True


def test_global_resolver_uses_active_profile_without_workflow_state():
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
        "value": 0x1234,
        "resolved_addr": 0x5000,
        "use_addr": 0x4000,
    }]

    workflow.resolve_globals_mlil(ctx)

    assert active_profile_calls == [ctx.view]
    assert global_plan_calls == [(ctx.view, ctx.mlil)]
    assert ctx.typed_globals == [(0xA43D70, "uint64_t")]
    assert ctx.view.session_data[workflow.GLOBAL_CONSTANT_RECEIPTS] == {0xA43D70: "uint64_t"}
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


def test_global_profile_hook_miss_does_not_fallback_to_default_resolver():
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
    assert workflow.GLOBAL_CONSTANT_RECEIPTS not in ctx.view.session_data
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


def test_string_decrypt_waits_for_branch_call_and_global_stability():
    ctx = FakeContext()

    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    assert workflow.string_decrypt_gate_mlil(ctx) is False

    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = False
    assert workflow.string_decrypt_gate_mlil(ctx) is False

    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = False
    assert workflow.string_decrypt_gate_mlil(ctx) is False

    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False


def test_string_decrypt_does_not_require_deflatten_stability():
    FakeWorkflowState.stable = True
    FakeWorkflowState.calls_stable = True
    FakeWorkflowState.globals_stable = True
    ctx = FakeContext()

    assert workflow.string_decrypt_gate_mlil(ctx) is True
    assert "dispatchthis_mlil_stable" not in ctx.view.session_data
    FakeWorkflowState.stable = False
    FakeWorkflowState.calls_stable = False
    FakeWorkflowState.globals_stable = False


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
    test_deflatten_waits_for_global_phase_stability()
    test_branch_resolver_reuses_branch_receipts_as_known_targets()
    test_branch_resolver_does_not_stabilize_unparsed_indirect_jumps()
    test_branch_resolver_does_not_stabilize_unparsed_later_jump_after_partial_mapping()
    test_branch_resolver_uses_function_llil_fallback_for_newly_discovered_jump()
    test_branch_resolver_schedules_tag_cleanup_once_while_pending()
    test_call_phase_submits_pending_function_llil_branches_before_call_work()
    test_call_resolver_uses_active_profile_without_workflow_state()
    test_call_profile_hook_miss_does_not_fallback_to_default_resolver()
    test_global_resolver_uses_active_profile_without_workflow_state()
    test_global_profile_hook_miss_does_not_fallback_to_default_resolver()
    test_global_resolver_does_not_stabilize_when_receipts_no_longer_verify()
    test_string_decrypt_waits_for_branch_call_and_global_stability()
    test_string_decrypt_does_not_require_deflatten_stability()
    test_cleanup_waits_for_deflatten_stability()
    test_cleanup_commits_when_deflatten_state_writes_are_nopped()
    test_cleanup_does_not_commit_when_no_deflatten_state_writes_are_nopped()
    test_noreturn_type_detection_and_fallthrough_callsite()
