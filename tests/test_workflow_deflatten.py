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


def fake_compute(_bv, func, mlil=None):
    calls.append(("compute", func.start, mlil))
    return [{"kind": "uncond", "state_tokens": {(0x1234, 8)}, "state_vars": {"state"}}]


def fake_apply(mlil, plans):
    calls.append(("apply", mlil, plans))
    return 1


sys.modules.setdefault(
    "plugins.DispatchThis.passes.medium.deflatten",
    types.SimpleNamespace(compute_redirections=fake_compute, apply_redirections_il=fake_apply),
)
sys.modules.setdefault(
    "plugins.DispatchThis.passes.medium.nop_pass",
    types.SimpleNamespace(clean_resolved_gadget_jumps=lambda *_args, **_kwargs: (0, 0, 0, 0)),
)
sys.modules.setdefault(
    "plugins.DispatchThis.passes.medium.indirect_calls",
    types.SimpleNamespace(apply_indirect_call_rewrites=lambda *_args, **_kwargs: 0, plan_indirect_calls=lambda *_args, **_kwargs: []),
)
sys.modules.setdefault(
    "plugins.DispatchThis.passes.medium.branch_conditions",
    types.SimpleNamespace(translate_indirect_branch_conditions=lambda *_args, **_kwargs: (None, 0, set())),
)
sys.modules.setdefault(
    "plugins.DispatchThis.passes.medium.phase_cleanup",
    types.SimpleNamespace(cleanup_phase_decode=lambda *_args, **_kwargs: 0, mlil_set_var_roots_before_sites=lambda *_args, **_kwargs: set()),
)
sys.modules.setdefault(
    "plugins.DispatchThis.passes.medium.global_constants",
    types.SimpleNamespace(CONST_SLOT_TYPE="uint64_t", plan_global_constant_slots=lambda *_args, **_kwargs: []),
)
sys.modules.setdefault(
    "plugins.DispatchThis.passes.low.gadget_llil",
    types.SimpleNamespace(
        apply_llil_jump_rewrites=lambda *_args, **_kwargs: 0,
        clear_resolved_indirect_branch_tags=lambda *_args, **_kwargs: None,
        resolve_llil_jump_plan=lambda _bv, _llil, gadget_map=None: branch_plan_calls.append(gadget_map) or [],
        schedule_resolved_indirect_branch_tag_cleanup=lambda *_args, **_kwargs: None,
    ),
)
sys.modules.setdefault(
    "plugins.DispatchThis.utils.log",
    types.SimpleNamespace(log_info=lambda _msg: None, log_warn=lambda _msg: None, log_debug=lambda _msg: None),
)
class FakeWorkflowState:
    receipts = {}
    marked_stable = False

    def __init__(self, _func):
        pass

    @staticmethod
    def unmapped_unresolved_sources(_func):
        return set()

    def branch_resolving_is_stable(self, _func):
        return False

    def branch_target_receipts(self):
        return self.receipts

    def branch_mutations_for(self, _resolved_targets):
        return {}

    def mark_branch_resolving_stable(self):
        FakeWorkflowState.marked_stable = True


sys.modules.setdefault(
    "plugins.DispatchThis.workflow_state",
    types.SimpleNamespace(FunctionWorkflowState=FakeWorkflowState),
)

spec = importlib.util.spec_from_file_location(
    "plugins.DispatchThis.workflow",
    ROOT / "plugins" / "DispatchThis" / "workflow.py",
)
workflow = importlib.util.module_from_spec(spec)
workflow.__package__ = "plugins.DispatchThis"
sys.modules[spec.name] = workflow
spec.loader.exec_module(workflow)


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
    FakeWorkflowState.marked_stable = False
    branch_plan_calls.clear()
    ctx = FakeContext()
    ctx.view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    ctx.llil = object()

    workflow.workflow_resolve_jumps_llil(ctx)

    assert branch_plan_calls == [FakeWorkflowState.receipts]
    assert FakeWorkflowState.marked_stable is True


if __name__ == "__main__":
    test_deflatten_workflow_runs_without_resolved_gadget_map()
    test_branch_resolver_reuses_branch_receipts_as_gadget_cache()
