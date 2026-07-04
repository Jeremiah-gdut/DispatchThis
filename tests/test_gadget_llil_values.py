import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

for name in (
    "plugins",
    "plugins.DispatchThis",
    "plugins.DispatchThis.passes",
    "plugins.DispatchThis.passes.low",
    "plugins.DispatchThis.utils",
):
    sys.modules.setdefault(name, types.ModuleType(name))
log_stub = sys.modules.setdefault("plugins.DispatchThis.utils.log", types.SimpleNamespace())
log_stub.log_debug = getattr(log_stub, "log_debug", lambda _msg: None)
log_stub.log_warn = getattr(log_stub, "log_warn", lambda _msg: None)
log_stub.log_error = getattr(log_stub, "log_error", lambda _msg: None)

spec = importlib.util.spec_from_file_location(
    "plugins.DispatchThis.passes.low.gadget_llil",
    ROOT / "plugins" / "DispatchThis" / "passes" / "low" / "gadget_llil.py",
)
gadget_llil = importlib.util.module_from_spec(spec)
gadget_llil.__package__ = "plugins.DispatchThis.passes.low"
sys.modules[spec.name] = gadget_llil
spec.loader.exec_module(gadget_llil)


class Op:
    def __init__(self, name):
        self.name = name


class Var:
    def __init__(self, reg, version):
        self.reg = reg
        self.version = version

    def __eq__(self, other):
        return isinstance(other, Var) and (self.reg, self.version) == (other.reg, other.version)

    def __hash__(self):
        return hash((self.reg, self.version))

    def __str__(self):
        return f"{self.reg}#{self.version}"


class Expr:
    def __init__(self, op, text=None, **attrs):
        self.operation = Op(op)
        self.text = text or op
        for key, value in attrs.items():
            setattr(self, key, value)

    def __str__(self):
        return self.text


class FakeSSA:
    def __init__(self, defs):
        self.defs = defs

    def get_ssa_reg_definition(self, var):
        return self.defs.get(var)


def const(value):
    return Expr("LLIL_CONST", hex(value), constant=value)


def reg(var):
    return Expr("LLIL_REG_SSA", str(var), src=var)


def partial(full_reg, partial_reg):
    return Expr("LLIL_REG_SSA_PARTIAL", f"{full_reg}.{partial_reg}", full_reg=full_reg, src=partial_reg, size=4)


def set_reg(src):
    return Expr("LLIL_SET_REG_SSA", f"set({src})", src=src)


def zx(src):
    return Expr("LLIL_ZX", f"zx({src})", src=src)


def bool_to_int():
    unknown_arg = partial(Var("x0", 6), "w0")
    cmp = Expr("LLIL_CMP_E", f"{unknown_arg} == 0", left=unknown_arg, right=const(0))
    return Expr("LLIL_BOOL_TO_INT", f"{cmp} ? 1 : 0", src=cmp)


def lsl(left, shift):
    return Expr("LLIL_LSL", f"{left} << {shift}", left=left, right=const(shift))


def test_bool_to_int_partial_reg_offsets_collect_both_targets():
    x9_42 = Var("x9", 42)
    ssa = FakeSSA({x9_42: set_reg(zx(bool_to_int()))})
    offset = lsl(zx(partial(x9_42, "w9")), 4)

    assert gadget_llil._reg_consts(None, ssa, offset) == {0, 0x10}


def test_zx_partial_copied_to_full_reg_offsets_collect_both_targets():
    x9_320 = Var("x9", 320)
    x9_321 = Var("x9", 321)
    ssa = FakeSSA({
        x9_320: set_reg(zx(bool_to_int())),
        x9_321: set_reg(zx(partial(x9_320, "w9"))),
    })
    offset = lsl(reg(x9_321), 7)

    assert gadget_llil._reg_consts(None, ssa, offset) == {0, 0x80}


if __name__ == "__main__":
    test_bool_to_int_partial_reg_offsets_collect_both_targets()
    test_zx_partial_copied_to_full_reg_offsets_collect_both_targets()
