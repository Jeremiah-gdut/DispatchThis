import sys
import types

import pytest

from binaryninja import Type

from conftest import load_plugin_module


semantics = load_plugin_module("plugins.DispatchThis.semantics")

class NativeType(Type):
    def __init__(self, shape, width):
        self.shape = shape
        self.width = width

    def __eq__(self, other):
        return isinstance(other, NativeType) and self.shape == other.shape and self.width == other.width


class BrokenType(Type):
    def __init__(self):
        pass

    @property
    def width(self):
        raise RuntimeError("width is unavailable")


class NonNativeType:
    width = 8


class Segment:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class View:
    def __init__(self):
        self.arch = types.SimpleNamespace(name="aarch64")
        self.data_vars = {}
        self.definitions = []
        self.segment = Segment(0x1000, 0x2000)

    def get_segment_at(self, address):
        return self.segment if self.segment.start <= address < self.segment.end else None

    def get_data_var_at(self, address):
        return self.data_vars.get(address)

    def define_user_data_var(self, address, data_type):
        self.definitions.append((address, data_type))
        self.data_vars[address] = types.SimpleNamespace(type=data_type)


class Function:
    name = "sub_4000"
    start = 0x4000

    def __init__(self):
        self.session_data = {}
        self.indirect_branches = []
        self.unresolved_indirect_branches = []


def run_global_provider(monkeypatch, result, view=None, call_stable=True):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.workflow_state")
    function = Function()
    view = View() if view is None else view
    provider = semantics.SampleSemantics(
        provider_id="native-global",
        name="Native global",
        api_version=semantics.CORE_API_VERSION,
        global_data=lambda _query: result,
    )
    state = workflow_state.FunctionWorkflowState(function)
    state.mark_branch_stable()
    if call_stable:
        state.mark_call_stable()
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    workflow.resolve_globals_mlil(
        types.SimpleNamespace(function=function, view=view, mlil=object())
    )
    return state, view


def test_external_global_fact_applies_its_exact_native_type(monkeypatch):
    data_type = NativeType(("pointer", "const", "array"), 16)
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch((semantics.GlobalDataFact(0x1000, data_type),)),
    )

    assert view.definitions == [(0x1000, data_type)]
    assert state.global_receipts == {0x1000: data_type}
    assert not state.global_stable()


@pytest.mark.parametrize(
    ("shape", "width"),
    (
        (("pointer", "const"), 8),
        (("array", "const", 4), 16),
        (("struct", "Record", "const"), 24),
        (("pointer", "const", "pointer", "const"), 8),
    ),
)
def test_external_global_fact_preserves_native_type_shape(monkeypatch, shape, width):
    data_type = NativeType(shape, width)
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch((semantics.GlobalDataFact(0x1000, data_type),)),
    )

    assert view.definitions == [(0x1000, data_type)]
    assert state.global_receipts[0x1000] is data_type


@pytest.mark.parametrize(
    ("slot_addr", "data_type"),
    (
        (0x1020, NativeType(("zero-width",), 0)),
        (0x3000, NativeType(("unmapped",), 8)),
    ),
)
def test_external_global_batch_rejects_bad_ranges_before_mutating(
    monkeypatch,
    slot_addr,
    data_type,
):
    view = View()
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch(
            (
                semantics.GlobalDataFact(0x1000, NativeType(("valid",), 8)),
                semantics.GlobalDataFact(slot_addr, data_type),
            )
        ),
        view,
    )

    assert view.definitions == []
    assert state.global_receipts == {}
    assert state.global_stable() is False


@pytest.mark.parametrize(
    "second_slot",
    (0x1000, 0x1008),
)
def test_external_global_batch_rejects_conflicts_atomically(monkeypatch, second_slot):
    first = NativeType(("first",), 16)
    second = NativeType(("second",), 8)
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch(
            (
                semantics.GlobalDataFact(0x1000, first),
                semantics.GlobalDataFact(second_slot, second),
            )
        ),
    )

    assert view.definitions == []
    assert state.global_receipts == {}


def test_external_global_fact_requires_an_exact_readback(monkeypatch):
    requested = NativeType(("requested",), 8)

    class WrongReadbackView(View):
        def define_user_data_var(self, address, _data_type):
            self.definitions.append((address, requested))
            self.data_vars[address] = types.SimpleNamespace(
                type=NativeType(("different",), 8)
            )

    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch((semantics.GlobalDataFact(0x1000, requested),)),
        WrongReadbackView(),
    )

    assert view.definitions == [(0x1000, requested)]
    assert state.global_receipts == {}
    assert state.global_stable() is False


def test_external_global_type_validation_fails_closed(monkeypatch):
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch((semantics.GlobalDataFact(0x1000, BrokenType()),)),
    )

    assert view.definitions == []
    assert state.global_receipts == {}
    assert not state.global_stable()


def test_external_global_provider_rejects_non_native_types(monkeypatch):
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch((semantics.GlobalDataFact(0x1000, NonNativeType()),)),
    )

    assert view.definitions == []
    assert state.global_receipts == {}
    assert not state.global_stable()


def test_external_global_provider_rejects_a_malformed_batch(monkeypatch):
    batch = semantics.CompleteBatch(())
    object.__setattr__(batch, "facts", [])
    state, view = run_global_provider(monkeypatch, batch)

    assert view.definitions == []
    assert state.global_receipts == {}
    assert not state.global_stable()


def test_global_batch_outcomes_do_not_share_stability(monkeypatch):
    state, view = run_global_provider(
        monkeypatch,
        semantics.Inconclusive("incomplete type proof"),
    )

    assert view.definitions == []
    assert state.global_stable() is False

    state, view = run_global_provider(monkeypatch, semantics.CompleteBatch(()))

    assert view.definitions == []
    assert state.global_stable() is True


def test_unstable_call_phase_prevents_global_mutation(monkeypatch):
    data_type = NativeType(("pointer",), 8)
    state, view = run_global_provider(
        monkeypatch,
        semantics.CompleteBatch((semantics.GlobalDataFact(0x1000, data_type),)),
        call_stable=False,
    )

    assert view.definitions == []
    assert state.global_receipts == {}
