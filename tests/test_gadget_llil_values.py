from importlib import import_module
from types import SimpleNamespace

from binaryninja import LowLevelILOperation, RegisterValueType

from conftest import load_plugin_module


gadget_llil = load_plugin_module("plugins.DispatchThis.passes.low.deinbr")
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
        self.operation = LowLevelILOperation.__members__.get(op, Op(op))
        self.text = text or op
        for key, value in attrs.items():
            setattr(self, key, value)

    def __str__(self):
        return self.text

    def traverse(self, visit):
        yield visit(self)
        for value in self.__dict__.values():
            if isinstance(value, Expr):
                yield from value.traverse(visit)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    if isinstance(child, Expr):
                        yield from child.traverse(visit)


class FakeSSA:
    def __init__(self, defs, instructions=(), memory_defs=None):
        self.defs = defs
        self.instructions = list(instructions)
        self.memory_defs = memory_defs or {}

    def get_ssa_reg_definition(self, var):
        return self.defs.get(var)

    def get_ssa_memory_definition(self, memory):
        return self.memory_defs.get(memory)

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


def set_reg(src, block=None):
    return Expr("LLIL_SET_REG_SSA", f"set({src})", src=src, il_basic_block=block)


def set_reg_partial(src, full_reg, dest="w0"):
    return Expr(
        "LLIL_SET_REG_SSA_PARTIAL",
        f"set_partial({src})",
        src=src,
        full_reg=full_reg,
        dest=dest,
    )


def phi(*src, instr_index=0, block=None):
    return Expr(
        "LLIL_REG_PHI",
        "phi",
        src=list(src),
        instr_index=instr_index,
        il_basic_block=block,
    )


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


def load(src, instr_index, src_memory=None, size=8):
    return Expr(
        "LLIL_LOAD_SSA",
        f"[{src}]",
        src=src,
        instr_index=instr_index,
        src_memory=src_memory,
        size=size,
    )


def store(dest, src, instr_index, src_memory=None, dest_memory=None, size=8):
    return Expr(
        "LLIL_STORE_SSA",
        f"[{dest}] = {src}",
        dest=dest,
        src=src,
        instr_index=instr_index,
        src_memory=src_memory,
        dest_memory=dest_memory,
        size=size,
    )


def stack_address(expr, offset):
    expr.possible_values = SimpleNamespace(
        type=RegisterValueType.StackFrameOffset,
        offset=offset,
    )
    return expr


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


def test_bool_to_int_offsets_fold_constant_state_compare():
    offset = lsl(bool_to_int_from_constant_cmp(), 4)

    assert llil_helpers.const_values(None, FakeSSA({}), offset) == {0}


def test_stack_spill_reload_constant_is_folded_without_vsa():
    sp_1 = Var("sp", 1)
    x8_23 = Var("x8", 23)
    stack_slot = stack_address(add(reg(sp_1), const(0x20)), -0x20)
    spill = store(stack_slot, const(0xA456F0), 10, src_memory=0, dest_memory=1)
    reload = load(stack_slot, 20, src_memory=1)
    ssa = FakeSSA({x8_23: set_reg(reload)}, [spill], {1: spill})

    assert llil_helpers.const_values(None, ssa, add(reg(x8_23), const(8))) == {0xA456F8}


def test_stack_reload_uses_memory_ssa_chain_not_latest_textual_store():
    stack_slot = stack_address(add(reg(Var("sp", 1)), const(0x20)), -0x20)
    other_slot = stack_address(add(reg(Var("sp", 1)), const(0x40)), -0x40)
    feeding = store(stack_slot, const(0x11), 10, src_memory=0, dest_memory=1)
    intervening = store(other_slot, const(0x33), 15, src_memory=1, dest_memory=2)
    other_path = store(stack_slot, const(0x22), 19, src_memory=0, dest_memory=3)
    reload = load(stack_slot, 20, src_memory=2)
    ssa = FakeSSA(
        {},
        [feeding, intervening, other_path],
        {1: feeding, 2: intervening, 3: other_path},
    )

    assert llil_helpers.const_values(None, ssa, reload) == {0x11}


def test_stack_reload_rejects_unknown_call_and_preserves_divergent_memory_phi():
    stack_slot = stack_address(add(reg(Var("sp", 1)), const(0x20)), -0x20)
    call = Expr("LLIL_CALL_SSA")
    unknown = load(stack_slot, 20, src_memory=9)
    after_call = load(stack_slot, 21, src_memory=3)
    left = store(stack_slot, const(0x11), 10, src_memory=0, dest_memory=1)
    right = store(stack_slot, const(0x22), 11, src_memory=0, dest_memory=2)
    phi = Expr("LLIL_MEM_PHI", src_memory=[1, 2], dest_memory=4)
    after_phi = load(stack_slot, 22, src_memory=4)
    ssa = FakeSSA({}, [left, right, call, phi], {1: left, 2: right, 3: call, 4: phi})

    assert llil_helpers.const_values(None, ssa, unknown) is None
    assert llil_helpers.const_values(None, ssa, after_call) is None
    assert llil_helpers.const_values(None, ssa, after_phi) == {0x11, 0x22}


def test_stack_reload_accepts_memory_phi_with_equal_values_from_distinct_stores():
    stack_slot = stack_address(add(reg(Var("sp", 1)), const(0x20)), -0x20)
    left = store(stack_slot, const(0x11), 10, src_memory=0, dest_memory=1)
    right = store(stack_slot, const(0x11), 11, src_memory=0, dest_memory=2)
    phi = Expr("LLIL_MEM_PHI", src_memory=[1, 2], dest_memory=3)
    reload = load(stack_slot, 20, src_memory=3)
    ssa = FakeSSA({}, [left, right, phi], {1: left, 2: right, 3: phi})

    assert llil_helpers.const_values(None, ssa, reload) == {0x11}


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
    bv = type("BV", (), {
        "arch": SimpleNamespace(instr_alignment=4),
        "is_offset_executable": lambda _self, _target: True,
    })()
    old_resolver = gadget_llil.resolve_llil_jump_targets
    gadget_llil.resolve_llil_jump_targets = lambda *_args: [0x3000, 0x2000, 0x3000]
    try:
        assert gadget_llil.resolve_llil_jump_plan(bv, llil) == [{
            "source": 0x2000,
            "dest_expr_index": 7,
            "targets": (0x2000, 0x3000),
            "jump_il": jump,
        }]
    finally:
        gadget_llil.resolve_llil_jump_targets = old_resolver


def test_resolve_llil_jump_plan_skips_verified_sources_and_parses_unknown_sources(monkeypatch):
    known_jump = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
    )
    unknown_jump = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=8),
        address=0x2100,
    )

    class FakeLlil(list):
        pass

    llil = FakeLlil([[known_jump, unknown_jump]])
    llil.ssa_form = object()
    bv = type("BV", (), {
        "arch": SimpleNamespace(instr_alignment=4),
        "is_offset_executable": lambda _self, _target: True,
    })()
    resolver_calls = []

    def resolve(_bv, _ssa, jump_il):
        resolver_calls.append(jump_il.address)
        return [jump_il.address + 0x1000]

    monkeypatch.setattr(gadget_llil, "resolve_llil_jump_targets", resolve)

    assert gadget_llil.resolve_llil_jump_plan(
        bv,
        llil,
        {0x2000: (0x3000,)},
    ) == [{
        "source": 0x2100,
        "dest_expr_index": 8,
        "targets": (0x3100,),
        "jump_il": unknown_jump,
    }]
    assert resolver_calls == [0x2100]


def test_transient_jump_shape_miss_is_debug_not_warning(monkeypatch):
    jump = Expr("LLIL_JUMP", address=0x2000)
    debug_messages = []
    warning_messages = []
    monkeypatch.setattr(gadget_llil, "parse_jump_gadget_targets", lambda *_args: None)
    monkeypatch.setattr(gadget_llil, "log_debug", debug_messages.append)
    monkeypatch.setattr(gadget_llil, "log_warn", warning_messages.append)

    assert gadget_llil.resolve_llil_jump_targets(object(), object(), jump) == []
    assert debug_messages == ["[gadget-llil] shape mismatch @ 0x2000"]
    assert warning_messages == []


def test_offset_validation_never_keeps_only_the_decodable_subset(monkeypatch):
    bv = type("BV", (), {
        "arch": SimpleNamespace(instr_alignment=4),
        "is_offset_executable": lambda _self, target: target == 0x3000,
    })()
    decoded = {0: 0x3000, 8: None}
    monkeypatch.setattr(
        gadget_llil,
        "resolve_indirect_jump_addr",
        lambda _bv, _slot, offset, _base, _key: decoded[offset],
    )

    assert gadget_llil._valid_offsets(bv, 0x1000, 0x20, 0x30, {0, 8}) is None


def test_jump_part_candidates_require_full_semantic_consensus():
    agreeing = [
        (0x1000, 0x20, 0x30, {0, 8}),
        (0x1000, 0x20, 0x30, {8, 0}),
    ]
    conflicting = [*agreeing, (0x2000, 0x20, 0x30, {0, 8})]

    assert gadget_llil._consensus_jump_parts(agreeing) == (
        0x1000,
        0x20,
        0x30,
        {0, 8},
    )
    assert gadget_llil._consensus_jump_parts(conflicting) is None


def test_cached_branch_targets_are_rejected_as_a_whole(monkeypatch):
    jump = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
        instr_index=0,
        expr_index=1,
    )

    class FakeLlil(list):
        pass

    llil = FakeLlil([[jump]])
    llil.ssa_form = object()
    bv = type("BV", (), {
        "arch": SimpleNamespace(instr_alignment=4),
        "is_offset_executable": lambda _self, target: target == 0x3000,
    })()
    monkeypatch.setattr(gadget_llil, "iter_llil_indirect_jumps", lambda _llil: [jump])

    assert gadget_llil.resolve_llil_jump_plan(
        bv,
        llil,
        {0x2000: (0x3000, 0x4000)},
    ) == []


def test_jump_rewrite_rejects_regenerated_jump_witness():
    owner = object()
    current = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
        instr_index=3,
        expr_index=9,
        function=owner,
    )
    stale = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
        instr_index=3,
        expr_index=9,
        function=object(),
    )

    class FakeLlil(list):
        def const_pointer(self, _size, target):
            return ("const", target)

        def replace_expr(self, *_args):
            raise AssertionError("stale plan must not rewrite")

    llil = FakeLlil([[current]])
    bv = type("BV", (), {
        "arch": SimpleNamespace(address_size=8),
    })()
    plan = [{
        "source": 0x2000,
        "dest_expr_index": 7,
        "targets": (0x3000,),
        "jump_il": stale,
    }]

    assert gadget_llil.apply_llil_jump_rewrites(bv, llil, plan) == 0
    assert len(plan) == 1


def test_same_source_jump_facts_fail_as_one_group(monkeypatch):
    first = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
    )
    second = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=8),
        address=0x2000,
    )

    class FakeLlil(list):
        pass

    llil = FakeLlil([[first, second]])
    llil.ssa_form = object()
    bv = type("BV", (), {
        "arch": SimpleNamespace(instr_alignment=4),
        "is_offset_executable": lambda _self, _target: True,
    })()
    monkeypatch.setattr(
        gadget_llil,
        "resolve_llil_jump_targets",
        lambda _bv, _ssa, jump: [0x3000] if jump is first else [],
    )

    assert gadget_llil.resolve_llil_jump_plan(bv, llil) == []


def test_llil_rewrite_discards_conflicting_same_source_plans():
    jump = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
    )

    class FakeLlil(list):
        def __init__(self):
            super().__init__([[jump]])
            self.replacements = []

        def replace_expr(self, expr_index, dest):
            self.replacements.append((expr_index, dest))

    llil = FakeLlil()
    bv = type("BV", (), {
        "arch": SimpleNamespace(address_size=8),
    })()
    plan = [
        {"source": 0x2000, "dest_expr_index": 7, "targets": (0x3000,)},
        {"source": 0x2000, "dest_expr_index": 8, "targets": (0x4000,)},
    ]

    assert gadget_llil.apply_llil_jump_rewrites(bv, llil, plan) == 0
    assert len(plan) == 2
    assert llil.replacements == []


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


def test_full_register_recovery_rejects_partial_write_definitions():
    x1_1 = Var("x1", 1)
    partial_definition = set_reg_partial(const(0x10), x1_1, "w1")
    ssa = FakeSSA({x1_1: partial_definition})
    expression = reg(x1_1)

    assert llil_helpers.peel_reg_definition(ssa, expression) is expression
    assert llil_helpers.const_values(None, ssa, expression) is None


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


def test_correlated_binding_does_not_merge_distinct_same_named_registers():
    class NamedRegister:
        def __eq__(self, other):
            return self is other

        __hash__ = object.__hash__

        def __str__(self):
            return "x1#1"

    first = NamedRegister()
    second = NamedRegister()
    bindings = {first: 0x10, str(first): 0x10}

    assert llil_helpers._bound_value(bindings, second) is None


def test_correlated_const_values_preserves_sibling_phi_arms():
    a0 = Var("x1", 1)
    a1 = Var("x1", 2)
    a = Var("x1", 3)
    b0 = Var("x2", 1)
    b1 = Var("x2", 2)
    b = Var("x2", 3)
    left = SimpleNamespace(start=10)
    right = SimpleNamespace(start=20)
    join = SimpleNamespace(
        start=30,
        incoming_edges=(
            SimpleNamespace(source=left),
            SimpleNamespace(source=right),
        ),
    )
    ssa = FakeSSA({
        a0: set_reg(const(1), left),
        a1: set_reg(const(2), right),
        a: phi(a0, a1, instr_index=7, block=join),
        b0: set_reg(const(10), left),
        b1: set_reg(const(20), right),
        # Reverse source-array order to prove correlation comes from CFG
        # provenance, not undocumented parallel-list indexing.
        b: phi(b1, b0, instr_index=8, block=join),
    })
    expr = add(reg(a), reg(b))

    assert llil_helpers.const_values(None, ssa, expr) == {11, 12, 21, 22}
    assert llil_helpers.correlated_const_values(None, ssa, expr) == {11, 22}


def test_ambiguous_multi_phi_never_falls_back_to_cartesian_candidates():
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

    assert llil_helpers.correlated_const_values(None, ssa, add(reg(a), reg(b))) == set()


def test_correlated_multi_phi_rejects_unknown_bound_arm_expression():
    a0 = Var("x1", 1)
    a1 = Var("x1", 2)
    a = Var("x1", 3)
    b0 = Var("x2", 1)
    b1 = Var("x2", 2)
    b = Var("x2", 3)
    left = SimpleNamespace(start=10)
    right = SimpleNamespace(start=20)
    join = SimpleNamespace(
        start=30,
        incoming_edges=(
            SimpleNamespace(source=left),
            SimpleNamespace(source=right),
        ),
    )
    ssa = FakeSSA({
        a0: set_reg(const(1), left),
        a1: set_reg(const(2), right),
        a: phi(a0, a1, instr_index=7, block=join),
        b0: set_reg(const(10), left),
        b1: set_reg(const(20), right),
        b: phi(b0, b1, instr_index=8, block=join),
    })
    expression = add(add(reg(a), reg(b)), Expr("LLIL_UNIMPL"))

    assert llil_helpers.correlated_const_values(None, ssa, expression) == set()


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
    assert llil_helpers.const_values(None, FakeSSA({}), reg(Var("x9", 1))) is None


def test_const_values_rejects_phi_when_any_semantic_arm_is_unknown():
    known = Var("x1", 1)
    unknown = Var("x1", 2)
    merged = Var("x1", 3)
    ssa = FakeSSA({
        known: set_reg(const(0x10)),
        unknown: set_reg(Expr("LLIL_UNIMPL")),
        merged: phi(known, unknown, instr_index=7),
    })

    assert llil_helpers.const_values(None, ssa, reg(merged)) is None


def test_const_values_rejects_phi_when_mask_domain_is_too_large_to_enumerate():
    known = Var("x1", 1)
    masked_unknown = Var("x1", 2)
    merged = Var("x1", 3)
    unresolved = Var("x9", 1)
    ssa = FakeSSA({
        known: set_reg(const(0x10)),
        masked_unknown: set_reg(and_expr(reg(unresolved), const(0x1FF))),
        merged: phi(known, masked_unknown, instr_index=7),
    })

    assert llil_helpers.const_values(None, ssa, reg(merged)) is None


def test_const_values_preserves_full_width_and_wraps_each_expression_width():
    low = Var("x1", 1)
    high = Var("x1", 2)
    merged = Var("x1", 3)
    low_value = const(0x10)
    high_value = const(0x1000000000010)
    low_value.size = high_value.size = 8
    ssa = FakeSSA({
        low: set_reg(low_value),
        high: set_reg(high_value),
        merged: phi(low, high, instr_index=7),
    })
    left = const(0xFF)
    right = const(1)
    left.size = right.size = 1
    wrapped = Expr("LLIL_ADD", left=left, right=right, size=1)

    assert llil_helpers.const_values(None, ssa, reg(merged)) == {
        0x10,
        0x1000000000010,
    }
    assert llil_helpers.const_values(None, FakeSSA({}), wrapped) == {0}


def test_const_values_evaluates_repeated_register_reads_per_expression_path():
    value = Var("x1", 1)
    ssa = FakeSSA({value: set_reg(const(3))})

    assert llil_helpers.const_values(None, ssa, add(reg(value), reg(value))) == {6}


def test_single_const_uses_signed_comparison_width():
    negative = const(0xFFFFFFFFFFFFFFFF)
    zero = const(0)
    negative.size = zero.size = 8
    comparison = Expr("LLIL_CMP_SLT", left=negative, right=zero)
    condition = Expr("LLIL_BOOL_TO_INT", src=comparison, size=1)

    assert llil_helpers._single_const(None, FakeSSA({}), condition) == 1


def test_llil_rewrite_does_not_remove_user_functions_from_low_pass():
    removed = []
    target_func = type("Func", (), {"start": 0x3000})()
    bv = type("BV", (), {
        "arch": type("Arch", (), {"address_size": 8})(),
        "get_function_at": lambda _self, _target: target_func,
        "remove_user_function": lambda _self, func: removed.append(func),
    })()

    jump = Expr(
        "LLIL_JUMP",
        dest=Expr("LLIL_REG_SSA", expr_index=7),
        address=0x2000,
        instr_index=0,
        expr_index=1,
    )

    class FakeLLIL(list):
        source_function = type("Func", (), {"start": 0x1000})()

        def __init__(self):
            super().__init__([[jump]])

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
        "jump_il": jump,
    }]

    assert gadget_llil.apply_llil_jump_rewrites(bv, FakeLLIL(), plan) == 1
    assert removed == []


if __name__ == "__main__":
    test_bool_to_int_partial_reg_offsets_collect_both_targets()
    test_zx_partial_copied_to_full_reg_offsets_collect_both_targets()
    test_unknown_bitmask_offsets_collect_small_mask_values()
    test_bool_to_int_offsets_fold_constant_state_compare()
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
