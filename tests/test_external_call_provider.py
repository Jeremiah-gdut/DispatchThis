import sys
import types

from binaryninja import FunctionType, MediumLevelILOperation, Type

from conftest import load_plugin_module


semantics = load_plugin_module("plugins.DispatchThis.semantics")


class Expr:
    def __init__(self, operation, expr_index, constant=None):
        self.operation = operation
        self.expr_index = expr_index
        self.constant = constant


class Call:
    def __init__(self):
        self.operation = MediumLevelILOperation.MLIL_CALL
        self.address = 0x4000
        self.instr_index = 0
        self.expr_index = 5
        self.dest = Expr(MediumLevelILOperation.MLIL_VAR, 6)
        self.params = [types.SimpleNamespace(expr_type=Type.int(8))]


class Mlil:
    def __init__(self, call):
        self.call = call
        self.instructions = [call]
        self.source_function = types.SimpleNamespace(name="sub_4000")
        self.replacements = []
        self.finalized = False
        self.ssa_generated = False
        call.function = self

    def __getitem__(self, index):
        return self.instructions[index]

    def const_pointer(self, _size, target):
        return Expr(MediumLevelILOperation.MLIL_CONST_PTR, 7, target)

    def replace_expr(self, expr_index, replacement):
        self.replacements.append((expr_index, replacement.constant))
        self.call.dest = replacement

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        self.ssa_generated = True


class Function:
    name = "sub_4000"
    start = 0x4000

    def __init__(self):
        self.session_data = {}
        self.indirect_branches = []
        self.unresolved_indirect_branches = []
        self.adjustments = {}
        self.adjustment_writes = []

    def get_call_type_adjustment(self, address):
        return self.adjustments.get(address)

    def set_call_type_adjustment(self, address, type_):
        self.adjustment_writes.append((address, type_))
        if type_ is None:
            self.adjustments.pop(address, None)
            return
        self.adjustments[address] = type_


def run_call_provider(monkeypatch, call_targets, callee_type=None):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.state")
    function = Function()
    call = Call()
    mlil = Mlil(call)
    callee_type = callee_type or FunctionType.create(
        ret=Type.int(8),
        params=[Type.int(8)],
    )
    callee = types.SimpleNamespace(start=0x5000, type=callee_type)
    view = types.SimpleNamespace(
        arch=types.SimpleNamespace(address_size=8),
        get_function_at=lambda address, _platform=None: callee if address == 0x5000 else None,
    )
    provider = semantics.SampleSemantics(
        provider_id="singleton-call",
        name="Singleton call",
        api_version=semantics.CORE_API_VERSION,
        call_targets=call_targets,
    )
    state = workflow_state.FunctionWorkflowState(function)
    state.mark_branch_stable()
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)

    workflow.resolve_calls_mlil(types.SimpleNamespace(function=function, view=view, mlil=mlil))
    return workflow, state, function, call, mlil, view


def test_external_singleton_call_fact_rewrites_and_adjusts_type(monkeypatch):
    callee_type = FunctionType.create(ret=Type.int(8), params=[Type.int(8)])
    _workflow, _state, function, _call, mlil, _view = run_call_provider(
        monkeypatch,
        lambda _query: semantics.CompleteBatch(
            (semantics.CallTargetFact(_query.mlil.call, (0x5000,)),)
        ),
        callee_type,
    )

    assert mlil.replacements == [(6, 0x5000)]
    assert mlil.finalized is True
    assert mlil.ssa_generated is True
    assert function.adjustments[0x4000].parameters[0].type == Type.int(8)


def test_external_multi_target_call_stays_indirect_without_receipts(monkeypatch):
    _workflow, state, function, call, mlil, _view = run_call_provider(
        monkeypatch,
        lambda _query: semantics.CompleteBatch(
            (semantics.CallTargetFact(_query.mlil.call, (0x5000, 0x6000)),)
        ),
    )

    assert call.dest.operation is MediumLevelILOperation.MLIL_VAR
    assert mlil.replacements == []
    assert function.adjustment_writes == []
    assert state.call_target_receipts == {}
    assert state.call_receipts == {}
    assert state.call_stable()
    assert not state.call_cleanup_needed()


def test_external_missing_call_slot_is_stable(monkeypatch):
    _workflow, state, function, call, mlil, _view = run_call_provider(monkeypatch, None)

    assert call.dest.operation is MediumLevelILOperation.MLIL_VAR
    assert mlil.replacements == []
    assert function.adjustment_writes == []
    assert state.call_stable()


def test_external_multi_target_fact_clears_a_stale_singleton_receipt(monkeypatch):
    callee_type = FunctionType.create(ret=Type.int(8), params=[Type.int(8)])
    calls = 0

    def call_targets(query):
        nonlocal calls
        calls += 1
        if calls == 1:
            return semantics.CompleteBatch(
                (semantics.CallTargetFact(query.mlil.call, (0x5000,)),)
            )
        return semantics.CompleteBatch(
            (semantics.CallTargetFact(query.mlil.call, (0x5000, 0x6000)),)
        )

    workflow, state, function, call, mlil, view = run_call_provider(
        monkeypatch,
        call_targets,
        callee_type,
    )
    call.dest = Expr(MediumLevelILOperation.MLIL_VAR, 6)

    workflow.resolve_calls_mlil(types.SimpleNamespace(function=function, view=view, mlil=mlil))

    assert call.dest.operation is MediumLevelILOperation.MLIL_VAR
    assert function.adjustments == {}
    assert function.adjustment_writes[-1] == (0x4000, None)
    assert state.call_target_receipts == {}
    assert state.call_receipts == {}
    assert not state.call_stable()


def test_external_multi_target_fact_preserves_a_changed_user_adjustment(monkeypatch):
    callee_type = FunctionType.create(ret=Type.int(8), params=[Type.int(8)])
    calls = 0

    def call_targets(query):
        nonlocal calls
        calls += 1
        if calls == 1:
            return semantics.CompleteBatch(
                (semantics.CallTargetFact(query.mlil.call, (0x5000,)),)
            )
        return semantics.CompleteBatch(
            (semantics.CallTargetFact(query.mlil.call, (0x5000, 0x6000)),)
        )

    workflow, state, function, call, mlil, view = run_call_provider(
        monkeypatch,
        call_targets,
        callee_type,
    )
    manual_type = FunctionType.create(ret=Type.int(16), params=[Type.int(8)])
    function.adjustments[0x4000] = manual_type
    call.dest = Expr(MediumLevelILOperation.MLIL_VAR, 6)

    workflow.resolve_calls_mlil(types.SimpleNamespace(function=function, view=view, mlil=mlil))

    assert function.adjustments == {0x4000: manual_type}
    assert len(function.adjustment_writes) == 1
    assert state.call_target_receipts == {}
    assert state.call_receipts == {}
    assert state.call_stable()


def test_external_call_omission_clears_a_stale_singleton_receipt(monkeypatch):
    callee_type = FunctionType.create(ret=Type.int(8), params=[Type.int(8)])
    calls = 0

    def call_targets(query):
        nonlocal calls
        calls += 1
        if calls == 1:
            return semantics.CompleteBatch(
                (semantics.CallTargetFact(query.mlil.call, (0x5000,)),)
            )
        return semantics.CompleteBatch(())

    workflow, state, function, call, mlil, view = run_call_provider(
        monkeypatch,
        call_targets,
        callee_type,
    )
    call.dest = Expr(MediumLevelILOperation.MLIL_VAR, 6)

    workflow.resolve_calls_mlil(types.SimpleNamespace(function=function, view=view, mlil=mlil))

    assert function.adjustments == {}
    assert function.adjustment_writes[-1] == (0x4000, None)
    assert state.call_target_receipts == {}
    assert state.call_receipts == {}
    assert not state.call_stable()


def test_external_call_omission_marks_the_phase_stable(monkeypatch):
    _workflow, state, function, call, mlil, _view = run_call_provider(
        monkeypatch,
        lambda _query: semantics.CompleteBatch(()),
    )

    assert call.dest.operation is MediumLevelILOperation.MLIL_VAR
    assert mlil.replacements == []
    assert function.adjustment_writes == []
    assert state.call_stable()


def test_external_inconclusive_call_batch_does_not_mark_stable(monkeypatch):
    _workflow, state, function, call, mlil, _view = run_call_provider(
        monkeypatch,
        lambda _query: semantics.Inconclusive("call graph scan exhausted"),
    )

    assert call.dest.operation is MediumLevelILOperation.MLIL_VAR
    assert mlil.replacements == []
    assert function.adjustment_writes == []
    assert not state.call_stable()


def test_external_call_provider_rejects_a_malformed_batch(monkeypatch):
    batch = semantics.CompleteBatch(())
    object.__setattr__(batch, "facts", [])
    _workflow, state, function, call, mlil, _view = run_call_provider(
        monkeypatch,
        lambda _query: batch,
    )

    assert call.dest.operation is MediumLevelILOperation.MLIL_VAR
    assert mlil.replacements == []
    assert function.adjustment_writes == []
    assert not state.call_stable()


def test_external_call_type_adjustment_is_idempotent_after_direct_receipt(monkeypatch):
    callee_type = FunctionType.create(ret=Type.int(8), params=[Type.int(8)])
    calls = 0

    def call_targets(query):
        nonlocal calls
        calls += 1
        if calls == 1:
            return semantics.CompleteBatch(
                (semantics.CallTargetFact(query.mlil.call, (0x5000,)),)
            )
        return semantics.CompleteBatch(())

    workflow, state, function, call, mlil, view = run_call_provider(
        monkeypatch,
        call_targets,
        callee_type,
    )

    workflow.resolve_calls_mlil(types.SimpleNamespace(function=function, view=view, mlil=mlil))

    assert len(function.adjustment_writes) == 1
    assert function.adjustments[0x4000].parameters[0].type == Type.int(8)
    assert state.call_stable()
