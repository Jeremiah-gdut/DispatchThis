from importlib import import_module
import types

import conftest  # noqa: F401
import pytest
from binaryninja import MediumLevelILOperation, RegisterValueType


mlil_helpers = import_module("plugins.DispatchThis.helpers.mlil")


class Expr:
    _next_index = 1

    def __init__(self, op, children=(), **attrs):
        self.operation = MediumLevelILOperation[op]
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
            type=RegisterValueType.ConstantValue,
            value=value,
        )
    return Expr("MLIL_VAR", **attrs)


def add(left, right):
    return Expr("MLIL_ADD", [left, right], left=left, right=right)


def xor(left, right):
    return Expr("MLIL_XOR", [left, right], left=left, right=right)


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


def call_like(op, dest):
    return Expr(op, [dest], dest=dest)


def test_operation_families_pair_native_enums_with_generated_names():
    families = (
        (mlil_helpers.ADDRESS_OF_OPERATIONS, mlil_helpers.ADDRESS_OF_OPS),
        (mlil_helpers.CALL_OPERATIONS, mlil_helpers.CALL_OPS),
        (mlil_helpers.CONST_OPERATIONS, mlil_helpers.CONST_OPS),
        (mlil_helpers.LOAD_OPERATIONS, mlil_helpers.LOAD_OPS),
        (mlil_helpers.LOAD_STRUCT_OPERATIONS, mlil_helpers.LOAD_STRUCT_OPS),
        (mlil_helpers.SET_VAR_OPERATIONS, mlil_helpers.SET_VAR_OPS),
        (mlil_helpers.SLOT_LOAD_OPERATIONS, mlil_helpers.SLOT_LOAD_OPS),
        (mlil_helpers.STORE_OPERATIONS, mlil_helpers.STORE_OPS),
    )

    for operations, names in families:
        assert all(isinstance(operation, MediumLevelILOperation) for operation in operations)
        assert names == tuple(operation.name for operation in operations)


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
    state = NamedVar("state")
    assert mlil_helpers.same_var(state, state)
    assert not mlil_helpers.same_var(NamedVar("state"), NamedVar("other"))

    assert mlil_helpers.var_from_expr(var("state")) == "state"
    assert mlil_helpers.var_from_expr(Expr("MLIL_VAR_FIELD", src="state")) == "state"
    ssa_var = types.SimpleNamespace(var="state")
    assert mlil_helpers.var_from_expr(Expr("MLIL_VAR_SSA", src=ssa_var)) == "state"
    assert mlil_helpers.var_from_expr(Expr("MLIL_VAR_SSA_FIELD", src=ssa_var)) == "state"
    assert mlil_helpers.var_from_expr(const(1)) is None
    assert mlil_helpers.addressed_var(Expr("MLIL_ADDRESS_OF", src="state")) == "state"
    assert (
        mlil_helpers.addressed_var(
            Expr("MLIL_ADDRESS_OF_FIELD", src="state", offset=4)
        )
        == "state"
    )

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


def test_instruction_write_detection_covers_partial_split_and_aliased_forms():
    ssa_state = types.SimpleNamespace(var="state")
    mutations = (
        Expr("MLIL_SET_VAR_FIELD", dest="state", offset=0, src=const(1)),
        Expr(
            "MLIL_SET_VAR_SPLIT",
            high="high",
            low="state",
            src=const(1),
            vars_written=("high", "state"),
        ),
        Expr(
            "MLIL_SET_VAR_ALIASED",
            dest=ssa_state,
            prev=ssa_state,
            src=const(1),
            vars_written=(ssa_state,),
        ),
        Expr(
            "MLIL_SET_VAR_ALIASED_FIELD",
            dest=ssa_state,
            prev=ssa_state,
            offset=0,
            src=const(1),
        ),
    )

    assert all(
        mlil_helpers.instruction_writes_variable(instruction, "state")
        for instruction in mutations
    )


def test_same_var_does_not_merge_distinct_variables_with_the_same_display_name():
    class NamedVariable:
        def __init__(self, identity):
            self.identity = identity

        def __eq__(self, other):
            return self is other

        __hash__ = object.__hash__

        def __str__(self):
            return "state"

    first = NamedVariable(1)
    second = NamedVariable(2)

    assert not mlil_helpers.same_var(first, second)


def test_instruction_read_detection_covers_split_and_aliased_forms():
    ssa_state = types.SimpleNamespace(var="state")
    reads = (
        Expr("MLIL_VAR_SPLIT", high="state", low="other", vars_read=("state", "other")),
        Expr("MLIL_VAR_SPLIT_SSA", high=ssa_state, low=ssa_state, vars_read=(ssa_state,)),
        Expr("MLIL_VAR_ALIASED", src=ssa_state),
        Expr("MLIL_VAR_ALIASED_FIELD", src=ssa_state, offset=0),
    )

    assert all(
        mlil_helpers.instruction_reads_variable(read, "state")
        for read in reads
    )


def test_address_may_alias_follows_variable_field_definitions_without_depth_cutoff():
    address = Expr("MLIL_ADDRESS_OF_FIELD", src="state", offset=0)
    definition = Expr(
        "MLIL_SET_VAR_FIELD",
        [address],
        dest="holder",
        offset=0,
        src=address,
    )
    expression = Expr("MLIL_VAR_FIELD", src="holder", offset=0)
    mlil = FakeMlil(defs={"holder": [definition]})

    assert mlil_helpers.expression_may_address_variable(mlil, expression, "state")


def test_address_may_escape_through_address_of_pointer_holder():
    state_address = Expr("MLIL_ADDRESS_OF_FIELD", src="state", offset=0)
    holder_definition = Expr(
        "MLIL_SET_VAR",
        [state_address],
        dest="holder",
        src=state_address,
    )
    holder_address = Expr("MLIL_ADDRESS_OF", src="holder")
    retain = Expr(
        "MLIL_CALL",
        [holder_address],
        params=[holder_address],
    )
    mlil = FakeMlil([retain], defs={"holder": [holder_definition]})

    assert mlil_helpers.variable_address_escapes(mlil, "state")


def test_address_escape_checker_shares_alias_work_across_effects_and_queries():
    class CountingMlil(FakeMlil):
        def __init__(self, instructions=(), defs=None):
            super().__init__(instructions, defs)
            self.definition_lookups = 0

        def get_var_definitions(self, variable):
            self.definition_lookups += 1
            return super().get_var_definitions(variable)

    mlil = CountingMlil((call(var("holder")), call(var("holder"))))
    address_escapes = mlil_helpers.address_escape_checker(mlil)

    assert not address_escapes("state")
    assert not address_escapes("other state")
    assert mlil.definition_lookups == 1

    assert not mlil_helpers.address_escape_checker(mlil)("state")
    assert mlil.definition_lookups == 2


def test_address_escape_checker_fails_closed_on_definition_lookup_error():
    class BrokenMlil(FakeMlil):
        def get_var_definitions(self, _variable):
            raise RuntimeError("definitions unavailable")

    address_escapes = mlil_helpers.address_escape_checker(
        BrokenMlil((call(var("holder")),)),
    )

    assert address_escapes("state")


def test_address_escape_checker_keeps_visited_wrappers_alive():
    destroyed = []

    class Node:
        def __init__(
            self,
            reads=(),
            addressed=(),
            tracked=False,
            operation=MediumLevelILOperation.MLIL_VAR,
        ):
            self.operation = operation
            self.vars_read = reads
            self.vars_address_taken = addressed
            self.tracked = tracked

        def traverse(self, visit):
            yield visit(self)

        def __del__(self):
            if self.tracked:
                destroyed.append(True)

    class Mlil:
        def __init__(self):
            self.instructions = [
                Node((1,), operation=MediumLevelILOperation.MLIL_CALL),
            ]

        def get_var_definitions(self, variable):
            if variable < 3:
                return [Node((variable + 1,), tracked=True)]
            if destroyed:
                return []
            return [Node(addressed=("state",), tracked=True)]

    assert mlil_helpers.address_escape_checker(Mlil())("state")


def test_scope_locality_checker_indexes_current_mlil_once():
    class Block(list):
        def __init__(self, start, *instructions):
            super().__init__(instructions)
            self.start = start

    class Mlil:
        def __init__(self):
            self.scans = 0
            self._blocks = (
                Block(1, Expr("MLIL_VAR", src="state", vars_read=("state",))),
                Block(2, Expr("MLIL_VAR", src="state", vars_read=("state",))),
            )

        @property
        def basic_blocks(self):
            self.scans += 1
            return self._blocks

    mlil = Mlil()
    variables_are_local = mlil_helpers.scope_locality_checker(mlil)

    assert not variables_are_local(("state",), {1})
    assert variables_are_local(("state",), {1, 2})
    assert mlil.scans == 1

    assert mlil_helpers.scope_locality_checker(mlil)(("state",), {1, 2})
    assert mlil.scans == 2


def test_scope_locality_checker_never_publishes_a_partial_index():
    class Block(list):
        def __init__(self, start, *instructions):
            super().__init__(instructions)
            self.start = start

    empty = Block(1)
    external_read = Block(
        2,
        Expr("MLIL_VAR", src="state", vars_read=("state",)),
    )

    class Mlil:
        scans = 0

        @property
        def basic_blocks(self):
            self.scans += 1
            if self.scans == 1:
                def broken_blocks():
                    yield empty
                    raise RuntimeError("incomplete block list")

                return broken_blocks()
            return (empty, external_read)

    mlil = Mlil()
    variables_are_local = mlil_helpers.scope_locality_checker(mlil)

    with pytest.raises(RuntimeError, match="incomplete block list"):
        variables_are_local(("state",), {1})

    assert not variables_are_local(("state",), {1})
    assert mlil.scans == 2


def test_address_alias_worklist_keeps_distinct_same_named_variables():
    class NamedVariable:
        def __init__(self, identity):
            self.identity = identity

        def __eq__(self, other):
            return self is other

        __hash__ = object.__hash__

        def __repr__(self):
            return "<var state>"

        def __str__(self):
            return "state"

    unrelated = NamedVariable(1)
    pointer = NamedVariable(2)
    address = Expr("MLIL_ADDRESS_OF", src="state")
    definitions = {
        unrelated: [Expr("MLIL_SET_VAR", src=const(0), dest=unrelated)],
        pointer: [Expr("MLIL_SET_VAR", [address], src=address, dest=pointer)],
    }
    expression = Expr(
        "MLIL_ADD",
        [var(unrelated), var(pointer)],
        left=var(unrelated),
        right=var(pointer),
    )
    mlil = FakeMlil(defs=definitions)

    assert mlil_helpers.expression_may_address_variable(mlil, expression, "state")


def test_iter_indirect_calls_skips_direct_constant_destinations():
    direct = call(const(0x5000))
    indirect = call(var("x0"))
    mlil = FakeMlil([direct, indirect, Expr("MLIL_SET_VAR")])

    assert list(mlil_helpers.iter_indirect_calls(mlil)) == [indirect]


def test_call_helpers_scan_call_like_and_direct_destinations():
    direct_calls = [
        call_like(op, const(0x5000 + index))
        for index, op in enumerate(mlil_helpers.CALL_OPS)
    ]
    value_dest = call(var("import", value=0x7000))
    indirect = call(var("x0"))
    mlil = FakeMlil([*direct_calls, value_dest, indirect, Expr("MLIL_SET_VAR")])

    assert list(mlil_helpers.iter_calls(mlil)) == [*direct_calls, value_dest, indirect]
    assert list(mlil_helpers.iter_calls(mlil, "MLIL_TAILCALL")) == [
        call for call in direct_calls if mlil_helpers.op_name(call) == "MLIL_TAILCALL"
    ]
    assert list(mlil_helpers.iter_direct_calls(mlil)) == [*direct_calls, value_dest]


def test_expression_scalar_value_reads_constants_value_sets_and_single_definitions():
    definition = set_var("target", const(0x1234), instr_index=11)
    other_a = set_var("ambiguous", const(1), instr_index=12)
    other_b = set_var("ambiguous", const(2), instr_index=13)
    mlil = FakeMlil(defs={"target": [definition], "ambiguous": [other_a, other_b]})

    assert mlil_helpers.expression_scalar_value(mlil, const(0x5000)) == 0x5000
    assert mlil_helpers.expression_scalar_value(mlil, var("import", value=0x6000)) == 0x6000
    assert mlil_helpers.expression_scalar_value(mlil, var("target")) == 0x1234
    assert mlil_helpers.expression_scalar_value(mlil, var("ambiguous")) is None
    assert mlil_helpers.expression_scalar_value(mlil, add(const(1), const(2))) is None


def test_operation_queries_scan_expression_trees():
    expr = add(load(const(0x1000)), var("x0"))

    assert mlil_helpers.expression_has_operation(
        expr,
        MediumLevelILOperation.MLIL_LOAD,
    )
    assert mlil_helpers.expression_has_operation(expr, "MLIL_LOAD")
    assert mlil_helpers.expression_has_operation(expr, ("MLIL_STORE", "MLIL_CONST_PTR"))
    assert not mlil_helpers.expression_has_operation(expr, "MLIL_STORE")


def test_operation_queries_can_follow_variable_definitions():
    definition = set_var("tmp", xor(const(1), const(2)), instr_index=11)
    mlil = FakeMlil(defs={"tmp": [definition]})

    assert not mlil_helpers.expression_has_operation(var("tmp"), "MLIL_XOR")
    assert mlil_helpers.expression_or_definitions_have_operation(
        mlil,
        var("tmp"),
        MediumLevelILOperation.MLIL_XOR,
    )
    assert mlil_helpers.expression_or_definitions_have_operation(mlil, var("tmp"), "MLIL_XOR")
    assert mlil_helpers.expression_or_definitions_have_operation(
        mlil,
        var("tmp"),
        ("MLIL_ADD", "MLIL_XOR"),
    )


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


def test_peel_var_definitions_rejects_multiple_or_partial_definitions():
    original = var("target")
    first = set_var("target", const(1), instr_index=41)
    second = set_var("target", const(2), instr_index=42)
    partial = Expr("MLIL_SET_VAR_FIELD", [const(3)], src=const(3))

    assert mlil_helpers.peel_var_definitions(
        FakeMlil(defs={"target": [first, second]}),
        original,
    ) is original
    assert mlil_helpers.peel_var_definitions(
        FakeMlil(defs={"target": [partial]}),
        original,
    ) is original


def test_fold_constant_value_folds_load_arithmetic_and_value_sets():
    bv = FakeBv()
    bv.memory[0x1000] = (0x40).to_bytes(8, "little")
    mlil = FakeMlil()

    assert mlil_helpers.fold_constant_value(bv, mlil, add(load(const(0x1000)), const(2))) == 0x42
    assert mlil_helpers.fold_constant_value(bv, mlil, var("key", value=5)) == 5


def test_fold_constant_value_requires_definition_consensus():
    agreeing = FakeMlil(defs={
        "key": [
            set_var("key", const(5), instr_index=1),
            set_var("key", add(const(2), const(3)), instr_index=2),
        ],
    })
    divergent = FakeMlil(defs={
        "key": [
            set_var("key", const(5), instr_index=1),
            set_var("key", const(6), instr_index=2),
        ],
    })

    assert mlil_helpers.fold_constant_value(FakeBv(), agreeing, var("key")) == 5
    assert mlil_helpers.fold_constant_value(FakeBv(), divergent, var("key")) is None


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


def test_iter_expressions_visits_each_current_expression_once():
    shared = const(1)
    first = add(shared, const(2))
    second = load(shared)

    expressions = list(mlil_helpers.iter_expressions(FakeMlil((first, second))))

    assert [node.operation.name for node in expressions] == [
        "MLIL_ADD",
        "MLIL_CONST_PTR",
        "MLIL_CONST_PTR",
        "MLIL_LOAD",
    ]


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


def test_fold_constant_value_honors_struct_offsets_and_cast_widths():
    bv = FakeBv()
    bv.memory[0x1020] = (0x55).to_bytes(8, "little")
    source_byte = Expr("MLIL_CONST", constant=0x80, size=1)

    assert mlil_helpers.fold_constant_value(
        bv,
        FakeMlil(),
        load_struct(const(0x1000), 0x20),
    ) == 0x55
    assert mlil_helpers.fold_constant_value(
        bv,
        FakeMlil(),
        Expr("MLIL_LOW_PART", [const(0x1234)], src=const(0x1234), size=1),
    ) == 0x34
    assert mlil_helpers.fold_constant_value(
        bv,
        FakeMlil(),
        Expr("MLIL_SX", [source_byte], src=source_byte, size=8),
    ) == 0xFFFFFFFFFFFFFF80

    byte_add = add(
        Expr("MLIL_CONST", constant=0xFF, size=1),
        Expr("MLIL_CONST", constant=1, size=1),
    )
    byte_add.size = 1
    byte_mul = Expr(
        "MLIL_MUL",
        [Expr("MLIL_CONST", constant=0x80, size=1), const(2)],
        left=Expr("MLIL_CONST", constant=0x80, size=1),
        right=const(2),
        size=1,
    )
    assert mlil_helpers.fold_constant_value(bv, FakeMlil(), byte_add) == 0
    assert mlil_helpers.fold_constant_value(bv, FakeMlil(), byte_mul) == 0


def test_mlil_store_detection_matches_constant_slot_destinations():
    slot_store = Expr("MLIL_STORE", [const(0xA43D70)], dest=const(0xA43D70))
    other_store = Expr("MLIL_STORE", [const(0xA43D80)], dest=const(0xA43D80))
    struct_store = Expr(
        "MLIL_STORE_STRUCT",
        [const(0xA43D00)],
        dest=const(0xA43D00),
        offset=0x70,
    )

    assert mlil_helpers.mlil_stores_to_address(
        FakeMlil([other_store, slot_store]), 0xA43D70
    )
    assert not mlil_helpers.mlil_stores_to_address(FakeMlil([other_store]), 0xA43D70)
    assert mlil_helpers.mlil_stores_to_address(FakeMlil([struct_store]), 0xA43D70)


def test_set_roots_before_returns_contiguous_assignment_instruction_indices():
    first = set_var("a", const(1), instr_index=11, expr_index=111, address=0x1000)
    second = set_var("b", const(2), instr_index=12, expr_index=112, address=0x1004)
    site = call(var("target"))
    site.address = 0x1008
    site.instr_index = 2
    later = set_var("c", const(3), instr_index=13, address=0x100C)
    mlil = FakeMlil([first, second, site, later])
    block = types.SimpleNamespace(start=0, end=4)
    mlil.basic_blocks = [block]
    site.il_basic_block = block

    assert mlil_helpers.set_roots_before(mlil, {0x1008}) == {11, 12}
    assert mlil_helpers.set_roots_before_instruction(mlil, site) == {11, 12}

    mlil.instructions.insert(1, nop(address=0x1002))
    block.end = 5
    assert mlil_helpers.set_roots_before(mlil, {0x1008}) == {12}
    assert mlil_helpers.set_roots_before_instruction(mlil, site) == {12}


if __name__ == "__main__":
    test_deflatten_profile_helpers_normalize_mlil_shapes()
    test_iter_indirect_calls_skips_direct_constant_destinations()
    test_call_helpers_scan_call_like_and_direct_destinations()
    test_expression_scalar_value_reads_constants_value_sets_and_single_definitions()
    test_operation_queries_scan_expression_trees()
    test_operation_queries_can_follow_variable_definitions()
    test_peel_var_definitions_tracks_set_var_trail()
    test_fold_constant_value_folds_load_arithmetic_and_value_sets()
    test_walk_expr_and_cleanup_roots_return_instruction_indices()
    test_iter_expressions_visits_each_current_expression_once()
    test_walk_expr_with_defs_expands_variable_definitions()
    test_const_address_and_slot_loads_support_global_slot_analysis()
    test_load_slot_offsets_follows_variable_offsets()
    test_mlil_store_detection_matches_constant_slot_destinations()
    test_set_roots_before_returns_contiguous_assignment_instruction_indices()
