from importlib import import_module
import types

import conftest  # noqa: F401


mlil_helpers = import_module("plugins.DispatchThis.helpers.mlil")


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    _next_index = 1

    def __init__(self, op, children=(), **attrs):
        self.operation = Op(op)
        self.children = list(children)
        self.expr_index = Expr._next_index
        Expr._next_index += 1
        self.__dict__.update(attrs)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out


class FakeBv:
    def __init__(self):
        self.memory = {}

    def read(self, addr, size):
        return self.memory.get(addr, b"")[:size]


class FakeMlil:
    def __init__(self, instructions=(), defs=None):
        self.instructions = list(instructions)
        self._defs = defs or {}
        self.basic_blocks = []

    def get_var_definitions(self, var):
        return self._defs.get(var, [])

    def __getitem__(self, index):
        return self.instructions[index]


def const(value):
    return Expr("MLIL_CONST_PTR", constant=value)


def var(name, value=None):
    attrs = {"src": name}
    if value is not None:
        attrs["value"] = types.SimpleNamespace(
            type=types.SimpleNamespace(name="ConstantValue"),
            value=value,
        )
    return Expr("MLIL_VAR", **attrs)


def add(left, right):
    return Expr("MLIL_ADD", [left, right], left=left, right=right)


def load(src, size=8):
    return Expr("MLIL_LOAD", [src], src=src, size=size)


def load_struct(src, offset, size=8):
    return Expr("MLIL_LOAD_STRUCT", [src], src=src, size=size, offset=offset)


def set_var(dest, src, instr_index, expr_index=None, address=0x1000):
    expr = Expr(
        "MLIL_SET_VAR",
        [src],
        dest=dest,
        src=src,
        instr_index=instr_index,
        address=address,
    )
    if expr_index is not None:
        expr.expr_index = expr_index
    return expr


def call(dest):
    return Expr("MLIL_CALL", [dest], dest=dest)


def nop(address=0x1000):
    return Expr("MLIL_NOP", address=address, instr_index=99)


def test_deflatten_profile_helpers_normalize_mlil_shapes():
    class NamedVar:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return self.name

    assert mlil_helpers.op_name(None) is None
    assert mlil_helpers.op_name(add(const(1), const(2))) == "MLIL_ADD"
    assert mlil_helpers.same_var(NamedVar("state"), NamedVar("state"))
    assert not mlil_helpers.same_var(NamedVar("state"), NamedVar("other"))

    assert mlil_helpers.var_from_expr(var("state")) == "state"
    assert mlil_helpers.var_from_expr(Expr("MLIL_VAR_FIELD", src="state")) == "state"
    ssa_var = types.SimpleNamespace(var="state")
    assert mlil_helpers.var_from_expr(Expr("MLIL_VAR_SSA", src=ssa_var)) == "state"
    assert mlil_helpers.var_from_expr(Expr("MLIL_VAR_FIELD_SSA", src=ssa_var)) == "state"
    assert mlil_helpers.var_from_expr(const(1)) is None

    assert mlil_helpers.state_token(Expr("MLIL_CONST", constant=0x123456789, size=4)) == (
        0x23456789,
        4,
    )
    assert mlil_helpers.state_token(Expr("MLIL_CONST", constant=0x123456, size=None), 2) == (
        0x3456,
        2,
    )
    assert mlil_helpers.state_token(Expr("MLIL_CONST", constant=-1)) == (
        0xFFFFFFFFFFFFFFFF,
        8,
    )


def test_iter_indirect_calls_skips_direct_constant_destinations():
    direct = call(const(0x5000))
    indirect = call(var("x0"))
    mlil = FakeMlil([direct, indirect, Expr("MLIL_SET_VAR")])

    assert list(mlil_helpers.iter_indirect_calls(mlil)) == [indirect]


def test_peel_var_definitions_tracks_set_var_trail():
    decoded = add(var("encoded"), const(7))
    definition = set_var("target", decoded, instr_index=42)
    trail = []

    result = mlil_helpers.peel_var_definitions(
        FakeMlil(defs={"target": [definition]}),
        var("target"),
        trail,
    )

    assert result is decoded
    assert trail == [definition]


def test_fold_constant_value_folds_load_arithmetic_and_value_sets():
    bv = FakeBv()
    bv.memory[0x1000] = (0x40).to_bytes(8, "little")
    mlil = FakeMlil()

    assert mlil_helpers.fold_constant_value(bv, mlil, add(load(const(0x1000)), const(2))) == 0x42
    assert mlil_helpers.fold_constant_value(bv, mlil, var("key", value=5)) == 5


def test_walk_expr_and_cleanup_roots_return_instruction_indices():
    left_def = set_var("left", const(1), instr_index=11, expr_index=111)
    right_def = set_var("right", const(2), instr_index=12, expr_index=112)
    expr = add(var("left"), var("right"))
    mlil = FakeMlil(defs={"left": [left_def], "right": [right_def]})

    assert [node.operation.name for node in mlil_helpers.walk_expr(expr)] == [
        "MLIL_ADD",
        "MLIL_VAR",
        "MLIL_VAR",
    ]
    assert mlil_helpers.cleanup_roots_for_expr(mlil, expr) == {11, 12}


def test_walk_expr_with_defs_expands_variable_definitions():
    definition = set_var("tmp", add(const(1), const(2)), instr_index=11)
    mlil = FakeMlil(defs={"tmp": [definition]})

    assert [node.operation.name for node in mlil_helpers.walk_expr_with_defs(mlil, var("tmp"))] == [
        "MLIL_VAR",
        "MLIL_ADD",
        "MLIL_CONST_PTR",
        "MLIL_CONST_PTR",
    ]


def test_const_address_and_slot_loads_support_global_slot_analysis():
    slot_load = set_var("slot", load(const(0xA43D70)), instr_index=11)
    mlil = FakeMlil(defs={"slot": [slot_load]})

    assert (
        mlil_helpers.constant_address(mlil, add(const(0xA00000), const(0x43D70)))
        == 0xA43D70
    )
    assert mlil_helpers.load_slot_address(mlil, var("slot")) == 0xA43D70
    assert (
        mlil_helpers.load_slot_address(mlil, load_struct(const(0xA00000), 0x43D70))
        == 0xA43D70
    )


def test_load_slot_offsets_follows_variable_offsets():
    slot_load = set_var("slot", load(const(0xA43D70)), instr_index=11, address=0x1000)
    base = set_var("base", add(var("slot"), const(0x20)), instr_index=12, address=0x1004)
    use = load(add(var("base"), const(4)))
    use.address = 0x1008
    mlil = FakeMlil([slot_load, base, use], {"slot": [slot_load], "base": [base]})

    assert mlil_helpers.load_slot_offsets(
        mlil,
        add(var("base"), const(4)),
        address_mask=0xFFFFFFFFFFFF,
    ) == [(0xA43D70, 0x24)]
    assert (use.src, 0x1008, 0xA43D70, 0x24) in list(
        mlil_helpers.iter_load_slot_offsets(mlil, address_mask=0xFFFFFFFFFFFF)
    )


def test_address_helpers_mask_only_when_requested():
    wide_addr = 0x1000000001234
    mlil = FakeMlil()

    assert mlil_helpers.constant_address(mlil, const(wide_addr)) == wide_addr
    assert (
        mlil_helpers.constant_address(mlil, const(wide_addr), address_mask=0xFFFFFFFFFFFF)
        == 0x1234
    )


def test_fold_constant_value_load_masks_only_when_requested():
    wide_addr = 0x1000000001000
    bv = FakeBv()
    bv.memory[wide_addr] = (0x40).to_bytes(8, "little")
    bv.memory[0x1000] = (0x99).to_bytes(8, "little")
    mlil = FakeMlil()

    assert mlil_helpers.fold_constant_value(bv, mlil, load(const(wide_addr))) == 0x40
    assert (
        mlil_helpers.fold_constant_value(
            bv,
            mlil,
            load(const(wide_addr)),
            load_address_mask=0xFFFFFFFFFFFF,
        )
        == 0x99
    )


def test_mlil_store_detection_matches_constant_slot_destinations():
    slot_store = Expr("MLIL_STORE", [const(0xA43D70)], dest=const(0xA43D70))
    other_store = Expr("MLIL_STORE", [const(0xA43D80)], dest=const(0xA43D80))

    assert mlil_helpers.mlil_stores_to_address(
        FakeMlil([other_store, slot_store]), 0xA43D70
    )
    assert not mlil_helpers.mlil_stores_to_address(FakeMlil([other_store]), 0xA43D70)


def test_set_roots_before_returns_contiguous_assignment_instruction_indices():
    first = set_var("a", const(1), instr_index=11, expr_index=111, address=0x1000)
    second = set_var("b", const(2), instr_index=12, expr_index=112, address=0x1004)
    site = call(var("target"))
    site.address = 0x1008
    later = set_var("c", const(3), instr_index=13, address=0x100C)
    mlil = FakeMlil([first, second, site, later])
    mlil.basic_blocks = [types.SimpleNamespace(start=0, end=4)]

    assert mlil_helpers.set_roots_before(mlil, {0x1008}) == {11, 12}

    mlil.instructions.insert(1, nop(address=0x1002))
    mlil.basic_blocks = [types.SimpleNamespace(start=0, end=5)]
    assert mlil_helpers.set_roots_before(mlil, {0x1008}) == {12}


if __name__ == "__main__":
    test_deflatten_profile_helpers_normalize_mlil_shapes()
    test_iter_indirect_calls_skips_direct_constant_destinations()
    test_peel_var_definitions_tracks_set_var_trail()
    test_fold_constant_value_folds_load_arithmetic_and_value_sets()
    test_walk_expr_and_cleanup_roots_return_instruction_indices()
    test_walk_expr_with_defs_expands_variable_definitions()
    test_const_address_and_slot_loads_support_global_slot_analysis()
    test_load_slot_offsets_follows_variable_offsets()
    test_mlil_store_detection_matches_constant_slot_destinations()
    test_set_roots_before_returns_contiguous_assignment_instruction_indices()
