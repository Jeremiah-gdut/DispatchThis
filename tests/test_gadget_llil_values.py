from importlib import import_module

from conftest import load_plugin_module


gadget_llil = load_plugin_module("plugins.DispatchThis.passes.low.gadget_llil")
llil_helpers = import_module("plugins.DispatchThis.helpers.llil")


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

    def __getitem__(self, index):
        return self.instructions[index]


def const(value):
    return Expr("LLIL_CONST", hex(value), constant=value)


def reg(var):
    return Expr("LLIL_REG_SSA", str(var), src=var)


def partial(full_reg, partial_reg):
    return Expr("LLIL_REG_SSA_PARTIAL", f"{full_reg}.{partial_reg}", full_reg=full_reg, src=partial_reg, size=4)


def set_reg(src):
    return Expr("LLIL_SET_REG_SSA", f"set({src})", src=src)


def phi(*src, instr_index=0):
    return Expr("LLIL_REG_PHI", "phi", src=list(src), instr_index=instr_index)


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

    assert llil_helpers.const_values(None, ssa, offset) == {0, 0x10}


def test_zx_partial_copied_to_full_reg_offsets_collect_both_targets():
    x9_320 = Var("x9", 320)
    x9_321 = Var("x9", 321)
    ssa = FakeSSA({
        x9_320: set_reg(zx(bool_to_int())),
        x9_321: set_reg(zx(partial(x9_320, "w9"))),
    })
    offset = lsl(reg(x9_321), 7)

    assert llil_helpers.const_values(None, ssa, offset) == {0, 0x80}


def test_unknown_bitmask_offsets_collect_small_mask_values():
    x10_3 = Var("x10", 3)
    x10_4 = Var("x10", 4)
    x10_5 = Var("x10", 5)
    ssa = FakeSSA({
        x10_4: set_reg(zx(and_expr(partial(x10_3, "w10"), const(1)))),
        x10_5: set_reg(zx(partial(x10_4, "w10"))),
    })
    offset = lsl(reg(x10_5), 5)

    assert llil_helpers.const_values(None, ssa, offset) == {0, 0x20}


def test_bool_to_int_offsets_do_not_prune_constant_state_compare():
    offset = lsl(bool_to_int_from_constant_cmp(), 4)

    assert llil_helpers.const_values(None, FakeSSA({}), offset) == {0, 0x10}


def test_stack_spill_reload_constant_is_folded_without_vsa():
    sp_1 = Var("sp", 1)
    x8_23 = Var("x8", 23)
    stack_slot = add(reg(sp_1), const(0x20))
    spill = store(stack_slot, const(0xA456F0), 10)
    reload = load(stack_slot, 20)
    ssa = FakeSSA({x8_23: set_reg(reload)}, [spill])

    assert llil_helpers.const_values(None, ssa, add(reg(x8_23), const(8))) == {0xA456F8}


def test_slot_add_candidates_require_single_table_base_key():
    slot_load = load(const(0x1000), 1)

    assert gadget_llil._slot_add_const_candidates(None, FakeSSA({}), add(slot_load, const(0x20))) == [
        (0x1000, 0x20)
    ]
    assert gadget_llil._slot_add_const_candidates(None, FakeSSA({}), add(slot_load, bool_to_int())) == []


def test_iter_indirect_jumps_skips_direct_const_destinations():
    indirect = Expr("LLIL_JUMP", dest=reg(Var("x1", 1)), address=0x1000)
    direct = Expr("LLIL_JUMP", dest=Expr("LLIL_CONST_PTR"), address=0x2000)
    tailcall = Expr("LLIL_TAILCALL", dest=reg(Var("x2", 1)), address=0x3000)
    llil = [[direct, indirect, Expr("LLIL_RET"), tailcall]]

    assert list(llil_helpers.iter_indirect_jumps(llil)) == [indirect, tailcall]


def test_resolve_llil_jump_plan_preserves_multi_target_branch_fact():
    jump = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
    )
    class FakeLlil(list):
        pass

    llil = FakeLlil([[jump]])
    llil.ssa_form = object()
    bv = type("BV", (), {"is_valid_offset": lambda _self, _target: True})()
    old_resolver = gadget_llil.resolve_llil_jump_targets
    gadget_llil.resolve_llil_jump_targets = lambda *_args: [0x3000, 0x2000, 0x3000]
    try:
        assert gadget_llil.resolve_llil_jump_plan(bv, llil) == [{
            "source": 0x2000,
            "dest_expr_index": 7,
            "targets": (0x2000, 0x3000),
            "newly_resolved": True,
        }]
    finally:
        gadget_llil.resolve_llil_jump_targets = old_resolver


def test_peel_reg_definition_follows_ssa_copies_until_expression():
    x1_1 = Var("x1", 1)
    x1_2 = Var("x1", 2)
    value = add(const(1), const(2))
    first = set_reg(value)
    second = set_reg(reg(x1_1))
    ssa = FakeSSA({x1_1: first, x1_2: second})
    trail = []

    assert llil_helpers.peel_reg_definition(ssa, reg(x1_2), trail) is value
    assert trail == [second, first]


def test_const_values_collect_phi_candidates():
    x1_1 = Var("x1", 1)
    x1_2 = Var("x1", 2)
    x1_3 = Var("x1", 3)
    ssa = FakeSSA({
        x1_1: set_reg(const(0x10)),
        x1_2: set_reg(const(0x20)),
        x1_3: phi(x1_1, x1_2, instr_index=7),
    })

    assert llil_helpers.const_values(None, ssa, reg(x1_3)) == {0x10, 0x20}


def test_correlated_const_values_preserves_sibling_phi_arms():
    a0 = Var("x1", 1)
    a1 = Var("x1", 2)
    a = Var("x1", 3)
    b0 = Var("x2", 1)
    b1 = Var("x2", 2)
    b = Var("x2", 3)
    ssa = FakeSSA({
        a0: set_reg(const(1)),
        a1: set_reg(const(2)),
        a: phi(a0, a1, instr_index=7),
        b0: set_reg(const(10)),
        b1: set_reg(const(20)),
        b: phi(b0, b1, instr_index=8),
    })
    expr = add(reg(a), reg(b))

    assert llil_helpers.const_values(None, ssa, expr) == {11, 12, 21, 22}
    assert llil_helpers.correlated_const_values(None, ssa, expr) == {11, 22}


def test_const_values_phi_cycle_returns_candidates_without_recursing_forever():
    x1_1 = Var("x1", 1)
    x1_2 = Var("x1", 2)
    x1_3 = Var("x1", 3)
    ssa = FakeSSA({
        x1_1: set_reg(const(0x10)),
        x1_2: set_reg(reg(x1_3)),
        x1_3: phi(x1_1, x1_2, instr_index=7),
    })

    assert llil_helpers.const_values(None, ssa, reg(x1_3)) == {0x10}


def test_const_values_keeps_phi_candidates_even_when_predicate_is_constant():
    if_block = type("Block", (), {"start": 0, "end": 1, "dominators": [], "outgoing_edges": []})()
    phi_block = type("Block", (), {"start": 1, "end": 2, "dominators": [], "outgoing_edges": []})()
    other_block = type("Block", (), {"start": 2, "end": 3, "dominators": [], "outgoing_edges": []})()
    phi_block.incoming_edges = [type("Edge", (), {"source": if_block})()]

    condition = Expr("LLIL_CMP_E", "1 == 1", left=const(1), right=const(1))
    if_instr = Expr("LLIL_IF", condition=condition, true=1, false=2)
    if_instr.il_basic_block = if_block
    phi_instr = Expr("LLIL_NOP")
    phi_instr.il_basic_block = phi_block
    other_instr = Expr("LLIL_NOP")
    other_instr.il_basic_block = other_block

    x1_1 = Var("x1", 1)
    x1_2 = Var("x1", 2)
    x1_3 = Var("x1", 3)
    live_def = set_reg(const(0x10))
    live_def.il_basic_block = if_block
    dead_def = set_reg(const(0x20))
    dead_def.il_basic_block = other_block
    phi_def = phi(x1_1, x1_2, instr_index=7)
    phi_def.il_basic_block = phi_block
    ssa = FakeSSA(
        {x1_1: live_def, x1_2: dead_def, x1_3: phi_def},
        [if_instr, phi_instr, other_instr],
    )

    assert llil_helpers.const_values(None, ssa, reg(x1_3)) == {0x10, 0x20}


def test_const_values_unresolved_value_falls_back_to_empty_set():
    assert llil_helpers.const_values(None, FakeSSA({}), reg(Var("x9", 1))) == set()


def test_llil_rewrite_does_not_remove_user_functions_from_low_pass():
    removed = []
    target_func = type("Func", (), {"start": 0x3000})()
    bv = type("BV", (), {
        "arch": type("Arch", (), {"address_size": 8})(),
        "get_function_at": lambda _self, _target: target_func,
        "remove_user_function": lambda _self, func: removed.append(func),
    })()

    class FakeLLIL:
        source_function = type("Func", (), {"start": 0x1000})()

        def const_pointer(self, _size, target):
            return ("const", target)

        def replace_expr(self, _expr_index, _dest):
            pass

        def finalize(self):
            pass

        def generate_ssa_form(self):
            pass

    plan = [{
        "source": 0x2000,
        "targets": (0x3000,),
        "dest_expr_index": 7,
    }]

    assert gadget_llil.apply_llil_jump_rewrites(bv, FakeLLIL(), plan) == 1
    assert removed == []


if __name__ == "__main__":
    test_bool_to_int_partial_reg_offsets_collect_both_targets()
    test_zx_partial_copied_to_full_reg_offsets_collect_both_targets()
    test_unknown_bitmask_offsets_collect_small_mask_values()
    test_bool_to_int_offsets_do_not_prune_constant_state_compare()
    test_stack_spill_reload_constant_is_folded_without_vsa()
    test_slot_add_candidates_require_single_table_base_key()
    test_iter_indirect_jumps_skips_direct_const_destinations()
    test_resolve_llil_jump_plan_preserves_multi_target_branch_fact()
    test_peel_reg_definition_follows_ssa_copies_until_expression()
    test_const_values_collect_phi_candidates()
    test_correlated_const_values_preserves_sibling_phi_arms()
    test_const_values_phi_cycle_returns_candidates_without_recursing_forever()
    test_const_values_keeps_phi_candidates_even_when_predicate_is_constant()
    test_const_values_unresolved_value_falls_back_to_empty_set()
    test_llil_rewrite_does_not_remove_user_functions_from_low_pass()
