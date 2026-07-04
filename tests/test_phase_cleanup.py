import sys
import types
import importlib.util
from pathlib import Path

sys.modules.setdefault("binaryninja", types.SimpleNamespace(ILSourceLocation=object))
ROOT = Path(__file__).resolve().parents[1]

for name in (
    "plugins",
    "plugins.DispatchThis",
    "plugins.DispatchThis.passes",
    "plugins.DispatchThis.passes.medium",
    "plugins.DispatchThis.utils",
):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules.setdefault(
    "plugins.DispatchThis.utils.log",
    types.SimpleNamespace(log_info=lambda _msg: None),
)

spec = importlib.util.spec_from_file_location(
    "plugins.DispatchThis.passes.medium.phase_cleanup",
    ROOT / "plugins" / "DispatchThis" / "passes" / "medium" / "phase_cleanup.py",
)
phase_cleanup = importlib.util.module_from_spec(spec)
phase_cleanup.__package__ = "plugins.DispatchThis.passes.medium"
sys.modules[spec.name] = phase_cleanup
spec.loader.exec_module(phase_cleanup)

_candidate_slice = phase_cleanup._candidate_slice
_drop_live_escapes = phase_cleanup._drop_live_escapes


class Op:
    def __init__(self, name):
        self.name = name


class Var:
    def __init__(self, name, version, source_type=None):
        self.name = name
        self.version = version
        self.source_type = source_type


class Ins:
    def __init__(self, idx, op, reads=(), writes=(), non_ssa=None):
        self.instr_index = idx
        self.operation = Op(op)
        self.vars_read = list(reads)
        self.vars_written = list(writes)
        self.non_ssa_form = non_ssa


class NonSSA:
    def __init__(self, idx):
        self.instr_index = idx


class FakeSSA:
    def __init__(self, uses=None, instructions=None):
        self.instructions = list(instructions or [])
        self.uses = uses or {}

    def get_ssa_var_uses(self, var):
        return self.uses.get(var, [])


def test_phi_only_use_does_not_keep_decode_candidate_live():
    a = Var("a", 1)
    b = Var("b", 1)
    c = Var("c", 1)

    candidate = Ins(1, "MLIL_SET_VAR_SSA", writes=[a])
    phi1 = Ins(2, "MLIL_VAR_PHI", reads=[a], writes=[b])
    phi2 = Ins(3, "MLIL_VAR_PHI", reads=[b], writes=[c])

    kept = _drop_live_escapes(FakeSSA({a: [phi1], b: [phi2]}), {1}, {1: candidate})

    assert kept == {1}


def test_phi_chain_with_real_use_keeps_decode_candidate_live():
    a = Var("a", 1)
    b = Var("b", 1)

    candidate = Ins(1, "MLIL_SET_VAR_SSA", writes=[a])
    phi = Ins(2, "MLIL_VAR_PHI", reads=[a], writes=[b])
    real_use = Ins(3, "MLIL_IF", reads=[b])

    kept = _drop_live_escapes(FakeSSA({a: [phi], b: [real_use]}), {1}, {1: candidate})

    assert kept == set()


def test_non_ssa_root_does_not_pull_unrelated_same_index_ssa_instruction():
    wanted = Ins(10, "MLIL_SET_VAR_SSA", non_ssa=NonSSA(1))
    colliding_phi = Ins(1, "MLIL_VAR_PHI")

    candidates, _ = _candidate_slice(FakeSSA(instructions=[colliding_phi, wanted]), {1})

    assert candidates == {10}


def test_stack_var_state_write_keeps_source_live():
    x = Var("x", 1)
    state = Var("state", 1, types.SimpleNamespace(name="StackVariableSourceType"))

    decode = Ins(1, "MLIL_SET_VAR_SSA", writes=[x])
    state_write = Ins(2, "MLIL_SET_VAR_SSA", reads=[x], writes=[state])
    ssa = FakeSSA({x: [state_write]}, [decode, state_write])

    candidates, by_index = _candidate_slice(ssa, {1, 2})
    kept = _drop_live_escapes(ssa, candidates, by_index)

    assert kept == set()


if __name__ == "__main__":
    test_phi_only_use_does_not_keep_decode_candidate_live()
    test_phi_chain_with_real_use_keeps_decode_candidate_live()
    test_non_ssa_root_does_not_pull_unrelated_same_index_ssa_instruction()
    test_stack_var_state_write_keeps_source_live()
