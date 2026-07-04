import sys
import types
import importlib.util
from pathlib import Path


class FakeLabel:
    pass


class FakeLoc:
    @staticmethod
    def from_instruction(instr):
        return ("loc", instr.expr_index)


sys.modules.setdefault(
    "binaryninja",
    types.SimpleNamespace(ILSourceLocation=FakeLoc, MediumLevelILLabel=FakeLabel),
)
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
    types.SimpleNamespace(
        log_info=lambda _msg: None,
        log_warn=lambda _msg: None,
        log_debug=lambda _msg: None,
    ),
)

spec = importlib.util.spec_from_file_location(
    "plugins.DispatchThis.passes.medium.nop_pass",
    ROOT / "plugins" / "DispatchThis" / "passes" / "medium" / "nop_pass.py",
)
nop_pass = importlib.util.module_from_spec(spec)
nop_pass.__package__ = "plugins.DispatchThis.passes.medium"
sys.modules[spec.name] = nop_pass
spec.loader.exec_module(nop_pass)


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    _next_index = 1

    def __init__(self, op, **attrs):
        self.operation = Op(op)
        self.expr_index = attrs.pop("expr_index", Expr._next_index)
        self.instr_index = attrs.pop("instr_index", self.expr_index)
        Expr._next_index += 1
        self.address = attrs.pop("address", 0x1000 + self.expr_index)
        self.__dict__.update(attrs)

    def traverse(self, visit):
        yield visit(self)
        for value in self.__dict__.values():
            if isinstance(value, Expr):
                yield from value.traverse(visit)


class FakeMlil:
    def __init__(self, instructions):
        self.instructions = list(instructions)
        self.replacements = []

    def replace_expr(self, expr_index, expr):
        self.replacements.append((expr_index, expr))

    def nop(self, loc):
        return ("nop", loc)

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        self.ssa_generated = True


def const(value):
    return Expr("MLIL_CONST", constant=value)


def set_var(dest, value):
    return Expr("MLIL_SET_VAR", dest=dest, src=const(value))


def test_ref_consts_reports_full_and_legacy_low32_values():
    ins = set_var("tmp", 0x6C5B6887819676A8)

    refs = nop_pass._ref_consts(ins)

    assert 0x6C5B6887819676A8 in refs
    assert 0x819676A8 in refs


def test_nop_state_writes_matches_full_width_state_constants():
    ins = set_var("tmp", 0x6C5B6887819676A8)
    mlil = FakeMlil([ins])

    count = nop_pass.nop_state_writes(mlil, {0x6C5B6887819676A8}, set())

    assert count == 1
    assert mlil.replacements == [(ins.expr_index, ("nop", ("loc", ins.expr_index)))]
    assert mlil.finalized is True
    assert mlil.ssa_generated is True


def test_nop_state_writes_keeps_legacy_low32_state_constant_match():
    ins = set_var("tmp", 0x6C5B6887819676A8)
    mlil = FakeMlil([ins])

    count = nop_pass.nop_state_writes(mlil, {0x819676A8}, set())

    assert count == 1


if __name__ == "__main__":
    test_ref_consts_reports_full_and_legacy_low32_values()
    test_nop_state_writes_matches_full_width_state_constants()
    test_nop_state_writes_keeps_legacy_low32_state_constant_match()
