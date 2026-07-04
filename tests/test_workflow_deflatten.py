import importlib.util
import sys
import types
from pathlib import Path


sys.modules.setdefault("binaryninja", types.SimpleNamespace(AnalysisContext=object))
ROOT = Path(__file__).resolve().parents[1]

for name in (
    "plugins",
    "plugins.DispatchThis",
    "plugins.DispatchThis.passes",
    "plugins.DispatchThis.passes.low",
    "plugins.DispatchThis.passes.medium",
    "plugins.DispatchThis.utils",
):
    sys.modules.setdefault(name, types.ModuleType(name))

calls = []
branch_plan_calls = []
branch_plan_results = {}
branch_iter_items = []
clear_tag_calls = []


def fake_compute(_bv, func, mlil=None):
    calls.append(("compute", func.start, mlil))
    return [{"kind": "uncond", "state_tokens": {(0x1234, 8)}, "state_vars": {"state"}}]


def fake_apply(mlil, plans):
    calls.append(("apply", mlil, plans))
    return 1


def fake_resolve_llil_jump_plan(_bv, llil, gadget_map=None):
    branch_plan_calls.append((llil, gadget_map))
    return branch_plan_results.get(llil, [])


class FakeWorkflowState:
    receipts = {}
    unmapped = set()
    marked_stable = False
    stable = False

    def __init__(self, _func):
        pass

    @staticmethod
    def unmapped_unresolved_sources(_func):
        return FakeWorkflowState.unmapped

    def branch_resolving_is_stable(self, _func):
        return self.stable

    def branch_target_receipts(self):
        return self.receipts

    def branch_mutations_for(self, _resolved_targets):
        return {}

    def mark_branch_resolving_stable(self):
        FakeWorkflowState.marked_stable = True


_FAKE_MODULES = {
    "plugins.DispatchThis.passes.medium.deflatten": types.SimpleNamespace(
        compute_redirections=fake_compute,
        apply_redirections_il=fake_apply,
    ),
    "plugins.DispatchThis.passes.medium.nop_pass": types.SimpleNamespace(
        clean_deflatten_state_writes=lambda *_args, **_kwargs: 0,
    ),
    "plugins.DispatchThis.passes.medium.indirect_calls": types.SimpleNamespace(
        apply_indirect_call_rewrites=lambda *_args, **_kwargs: 0,
        plan_indirect_calls=lambda *_args, **_kwargs: [],
    ),
    "plugins.DispatchThis.passes.medium.branch_conditions": types.SimpleNamespace(
        translate_indirect_branch_conditions=lambda *_args, **_kwargs: (None, 0, set()),
    ),
    "plugins.DispatchThis.passes.medium.phase_cleanup": types.SimpleNamespace(
        cleanup_phase_decode=lambda *_args, **_kwargs: 0,
        mlil_set_var_roots_before_sites=lambda *_args, **_kwargs: set(),
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

_MISSING = object()
_saved_modules = {name: sys.modules.get(name, _MISSING) for name in _FAKE_MODULES}
_saved_modules["plugins.DispatchThis.workflow"] = sys.modules.get("plugins.DispatchThis.workflow", _MISSING)
sys.modules.update(_FAKE_MODULES)

spec = importlib.util.spec_from_file_location(
    "plugins.DispatchThis.workflow",
    ROOT / "plugins" / "DispatchThis" / "workflow.py",
)
workflow = importlib.util.module_from_spec(spec)
workflow.__package__ = "plugins.DispatchThis"
sys.modules[spec.name] = workflow
spec.loader.exec_module(workflow)
for name, module in _saved_modules.items():
    if module is _MISSING:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


class FakeContext:
    def __init__(self):
        self.function = types.SimpleNamespace(start=0x9556D8, name="sub_9556d8")
        self.view = types.SimpleNamespace(session_data={"dispatchthis_llil_stable": {self.function.start: True}})
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


def test_deflatten_workflow_runs_without_resolved_gadget_map():
    ctx = FakeContext()

    workflow.workflow_deflatten_mlil(ctx)

    assert calls[0] == ("compute", ctx.function.start, ctx.mlil)
    assert calls[1][0] == "apply"
    assert ctx.committed is True
    assert ctx.view.session_data["dispatchthis_mlil_stable"][ctx.function.start] is True


def test_branch_resolver_reuses_branch_receipts_as_gadget_cache():
    FakeWorkflowState.receipts = {0x1000: (0x2000, 0x3000)}
    FakeWorkflowState.unmapped = {0x1000}
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items.clear()
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = object()

    workflow.workflow_resolve_jumps_llil(ctx)

    assert [gadget_map for _llil, gadget_map in branch_plan_calls] == [FakeWorkflowState.receipts]


def test_branch_resolver_does_not_stabilize_unparsed_indirect_jumps():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
    branch_plan_calls.clear()
    branch_plan_results.clear()
    branch_iter_items[:] = [types.SimpleNamespace(address=0x1000)]
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = object()

    workflow.workflow_resolve_jumps_llil(ctx)

    assert [gadget_map for _llil, gadget_map in branch_plan_calls] == [{}]
    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


def test_branch_resolver_does_not_stabilize_unparsed_later_jump_after_partial_mapping():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
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

    workflow.workflow_resolve_jumps_llil(ctx)

    assert [gadget_map for _llil, gadget_map in branch_plan_calls] == [{}]
    assert FakeWorkflowState.marked_stable is False
    branch_iter_items.clear()


def test_branch_resolver_uses_function_llil_fallback_for_newly_discovered_jump():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = {0x2000}
    FakeWorkflowState.marked_stable = False
    FakeWorkflowState.stable = False
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

    workflow.workflow_resolve_jumps_llil(ctx)

    assert [llil for llil, _gadget_map in branch_plan_calls] == ["function-llil"]
    branch_iter_items.clear()
    branch_plan_results.clear()


def test_branch_resolver_schedules_tag_cleanup_once_while_pending():
    FakeWorkflowState.receipts = {}
    FakeWorkflowState.unmapped = set()
    FakeWorkflowState.stable = True
    clear_tag_calls.clear()
    events = []
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
        add_analysis_completion_event=events.append,
        get_function_at=lambda start: ctx.function if start == ctx.function.start else None,
    )

    workflow.workflow_resolve_jumps_llil(ctx)
    workflow.workflow_resolve_jumps_llil(ctx)

    assert len(events) == 1
    assert ctx.view.session_data["dispatchthis_tag_cleanup_pending"] == {ctx.function.start}
    events[0]()
    assert clear_tag_calls[-1] is ctx.function
    assert ctx.view.session_data["dispatchthis_tag_cleanup_pending"] == set()
    FakeWorkflowState.stable = False


def test_noreturn_type_detection_and_fallthrough_callsite():
    block = types.SimpleNamespace(start=0, end=2, outgoing_edges=[])
    first = types.SimpleNamespace(instr_index=10, il_basic_block=block)
    call = types.SimpleNamespace(instr_index=11, il_basic_block=block)
    mlil = [first, call]

    assert workflow._type_is_noreturn("void() __noreturn")
    assert workflow._call_has_fallthrough(mlil, first)
    assert not workflow._call_has_fallthrough(mlil, call)

if __name__ == "__main__":
    test_deflatten_workflow_runs_without_resolved_gadget_map()
    test_branch_resolver_reuses_branch_receipts_as_gadget_cache()
    test_branch_resolver_does_not_stabilize_unparsed_indirect_jumps()
    test_branch_resolver_does_not_stabilize_unparsed_later_jump_after_partial_mapping()
    test_branch_resolver_uses_function_llil_fallback_for_newly_discovered_jump()
    test_branch_resolver_schedules_tag_cleanup_once_while_pending()
    test_noreturn_type_detection_and_fallthrough_callsite()
