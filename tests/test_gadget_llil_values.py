from conftest import load_plugin_module


gadget_llil = load_plugin_module("plugins.DispatchThis.passes.low.gadget_llil")


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
    def __init__(self, defs, instructions=()):
        self.defs = defs
        self.instructions = list(instructions)

    def get_ssa_reg_definition(self, var):
        return self.defs.get(var)

    def __iter__(self):
        return iter([self.instructions])


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


def bool_to_int_from_constant_cmp():
    cmp = Expr("LLIL_CMP_SLT", "0 s< 0", left=const(0), right=const(0))
    return Expr("LLIL_BOOL_TO_INT", f"{cmp} ? 1 : 0", src=cmp)


def lsl(left, shift):
    return Expr("LLIL_LSL", f"{left} << {shift}", left=left, right=const(shift))


def add(left, right):
    return Expr("LLIL_ADD", f"{left} + {right}", left=left, right=right)


def and_expr(left, right):
    return Expr("LLIL_AND", f"{left} & {right}", left=left, right=right)


def load(src, instr_index):
    return Expr("LLIL_LOAD_SSA", f"[{src}]", src=src, instr_index=instr_index)


def store(dest, src, instr_index):
    return Expr("LLIL_STORE_SSA", f"[{dest}] = {src}", dest=dest, src=src, instr_index=instr_index)


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


def test_unknown_bitmask_offsets_collect_small_mask_values():
    x10_3 = Var("x10", 3)
    x10_4 = Var("x10", 4)
    x10_5 = Var("x10", 5)
    ssa = FakeSSA({
        x10_4: set_reg(zx(and_expr(partial(x10_3, "w10"), const(1)))),
        x10_5: set_reg(zx(partial(x10_4, "w10"))),
    })
    offset = lsl(reg(x10_5), 5)

    assert gadget_llil._reg_consts(None, ssa, offset) == {0, 0x20}


def test_bool_to_int_offsets_do_not_prune_constant_state_compare():
    offset = lsl(bool_to_int_from_constant_cmp(), 4)

    assert gadget_llil._reg_consts(None, FakeSSA({}), offset) == {0, 0x10}


def test_stack_spill_reload_constant_is_folded_without_vsa():
    sp_1 = Var("sp", 1)
    x8_23 = Var("x8", 23)
    stack_slot = add(reg(sp_1), const(0x20))
    spill = store(stack_slot, const(0xA456F0), 10)
    reload = load(stack_slot, 20)
    ssa = FakeSSA({x8_23: set_reg(reload)}, [spill])

    assert gadget_llil._reg_const(None, ssa, add(reg(x8_23), const(8))) == 0xA456F8


if __name__ == "__main__":
    test_bool_to_int_partial_reg_offsets_collect_both_targets()
    test_zx_partial_copied_to_full_reg_offsets_collect_both_targets()
    test_unknown_bitmask_offsets_collect_small_mask_values()
    test_bool_to_int_offsets_do_not_prune_constant_state_compare()
    test_stack_spill_reload_constant_is_folded_without_vsa()
