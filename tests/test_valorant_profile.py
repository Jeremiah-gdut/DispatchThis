import types
from importlib import import_module

import conftest  # noqa: F401


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
        self.arch = types.SimpleNamespace(address_size=8)
        self.memory = {}
        self.valid_offsets = set()
        self.sections = []
        self.symbols = {}
        self.data_vars = {}

    def read(self, addr, size):
        value = self.memory.get(addr)
        if value is None:
            return b""
        return value.to_bytes(8, "little")[:size]

    def is_valid_offset(self, addr):
        return addr in self.valid_offsets

    def get_sections_at(self, addr):
        return [
            types.SimpleNamespace(name=name)
            for start, end, name in self.sections
            if start <= addr < end
        ]

    def get_symbol_at(self, _addr):
        return self.symbols.get(_addr)

    def get_function_at(self, _addr):
        return None


class FakeSSA:
    def __init__(self, defs, instructions=()):
        self.defs = defs
        self.instructions = list(instructions)

    def get_ssa_reg_definition(self, var):
        return self.defs.get(var)

    def __iter__(self):
        return iter([self.instructions])


class FakeLlil(list):
    pass


class FakeMlil:
    def __init__(self, instructions, defs, ssa_defs=None):
        self.instructions = list(instructions)
        self.defs = defs
        self.ssa_defs = ssa_defs or {}

    def get_var_definitions(self, var):
        return self.defs.get(var, [])

    def get_ssa_var_definition(self, var):
        return self.ssa_defs.get(var)


def const(op, value):
    return Expr(f"{op}_CONST", constant=value)


def reg(var):
    return Expr("LLIL_REG_SSA", src=var)


def var(name):
    return Expr("MLIL_VAR", src=name)


def var_ssa(name):
    return Expr("MLIL_VAR_SSA", src=name)


def set_reg(src, instr_index=0):
    return Expr("LLIL_SET_REG_SSA", [src], src=src, instr_index=instr_index)


def set_var(dest, src, instr_index, address=0x4010):
    return Expr("MLIL_SET_VAR", [src], dest=dest, src=src, instr_index=instr_index, address=address)


def set_var_ssa(src, instr_index, address=0x4010):
    return Expr("MLIL_SET_VAR_SSA", [src], src=src, instr_index=instr_index, address=address)


def phi(*src, instr_index=0):
    return Expr("LLIL_REG_PHI", src=list(src), instr_index=instr_index)


def unary(op, src):
    return Expr(op, [src], src=src)


def binary(op, left, right):
    return Expr(op, [left, right], left=left, right=right)


def load(op, src, size=8, address=0x4010):
    return Expr(f"{op}_LOAD", [src], src=src, size=size, address=address)


def store(dest, src, instr_index):
    return Expr("LLIL_STORE_SSA", [dest, src], dest=dest, src=src, instr_index=instr_index)


def qword(bv, addr, value):
    bv.memory[addr] = value & 0xFFFFFFFFFFFFFFFF


def test_branch_profile_resolves_main_two_target_jump():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    table_a = 0x1000
    table_b = 0x2000
    targets = {2: 0x3000, 0x21: 0x4000}

    for index, target in targets.items():
        offset = index << 3
        entry_a = 0
        tail = (index + index * ((-entry_a & valorant.U64) ^ valorant.MAIN_BRANCH_KEY)) ^ valorant.MAIN_BRANCH_KEY
        entry_b = (target - tail - 1) & valorant.U64
        qword(bv, table_a + offset, entry_a)
        qword(bv, table_b + offset + 1, entry_b)
        bv.valid_offsets.add(target)
        bv.sections.append((target, target + 4, ".text"))

    idx_2 = Var("x9", 1)
    idx_21 = Var("x9", 2)
    idx = Var("x9", 3)
    dst = Var("x8", 1)
    idx_expr = reg(idx)
    offset = binary("LLIL_LSL", idx_expr, const("LLIL", 3))
    entry_a = load("LLIL", binary("LLIL_ADD", const("LLIL", table_a), offset))
    tail = binary(
        "LLIL_XOR",
        binary(
            "LLIL_ADD",
            idx_expr,
            binary(
                "LLIL_MUL",
                idx_expr,
                binary("LLIL_XOR", unary("LLIL_NEG", entry_a), const("LLIL", valorant.MAIN_BRANCH_KEY)),
            ),
        ),
        const("LLIL", valorant.MAIN_BRANCH_KEY),
    )
    entry_b = load("LLIL", binary(
        "LLIL_ADD",
        binary("LLIL_ADD", const("LLIL", table_b), offset),
        const("LLIL", 1),
    ))
    dest = binary("LLIL_ADD", binary("LLIL_ADD", entry_b, tail), const("LLIL", 1))
    defs = {
        idx_2: set_reg(const("LLIL", 2), 1),
        idx_21: set_reg(const("LLIL", 0x21), 2),
        idx: phi(idx_2, idx_21, instr_index=3),
        dst: set_reg(dest, 4),
    }
    jump = Expr("LLIL_JUMP", [reg(dst)], dest=reg(dst), address=0x6C5F6C)
    jump.ssa_form = jump
    il = FakeLlil([[jump]])
    il.ssa_form = FakeSSA(defs)

    assert valorant.resolve_branch_gadget(bv, il) == [{
        "source": 0x6C5F6C,
        "dest_expr_index": jump.dest.expr_index,
        "targets": (0x3000, 0x4000),
        "newly_resolved": True,
    }]
    assert valorant.resolve_branch_gadget(bv, il, {0x6C5F6C: (0x3000, 0xDEAD)}) == [{
        "source": 0x6C5F6C,
        "dest_expr_index": jump.dest.expr_index,
        "targets": (0x3000, 0x4000),
        "newly_resolved": False,
    }]


def test_call_profile_accepts_text_target_without_existing_function():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.valid_offsets.add(0x5000)
    bv.sections.append((0x5000, 0x5100, ".text"))
    qword(bv, 0x1000, 0x4FF0)

    decode = binary("MLIL_ADD", load("MLIL", const("MLIL", 0x1000)), const("MLIL", 0x10))
    decode_def = set_var("target", decode, instr_index=7, address=0x3000)
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4000)
    il = FakeMlil([decode_def, call_il], {"target": [decode_def]})

    assert valorant.resolve_call_gadget(bv, il) == [{
        "call_il": call_il,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": decode_def,
        "cleanup_roots": {7},
    }]


def test_call_profile_follows_ssa_call_destination():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.valid_offsets.add(0x6000)
    bv.sections.append((0x6000, 0x6100, ".text"))
    qword(bv, 0x2000, 0x5FE0)

    decode = binary("MLIL_ADD", load("MLIL", const("MLIL", 0x2000)), const("MLIL", 0x20))
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4100)
    call_il.ssa_form = Expr("MLIL_CALL_SSA", [var_ssa("target#1")], dest=var_ssa("target#1"), address=0x4100)
    ssa = FakeMlil([], {}, {"target#1": set_var_ssa(decode, instr_index=11, address=0x3100)})
    il = FakeMlil([call_il], {})
    il.ssa_form = ssa

    assert valorant.resolve_call_gadget(bv, il) == [{
        "call_il": call_il,
        "call_addr": 0x4100,
        "target": 0x6000,
        "decode_def": None,
        "cleanup_roots": set(),
    }]


def test_call_profile_accepts_external_symbol_target():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    target = 0x8956BB63505153E0
    bv = FakeBv()
    bv.symbols[target] = types.SimpleNamespace(name="fork")

    decode_def = set_var("target", const("MLIL", target), instr_index=12, address=0x3200)
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4200)
    il = FakeMlil([decode_def, call_il], {"target": [decode_def]})

    assert valorant.resolve_call_gadget(bv, il) == [{
        "call_il": call_il,
        "call_addr": 0x4200,
        "target": target,
        "decode_def": decode_def,
        "cleanup_roots": {12},
    }]


def test_global_constant_profile_plans_qword_data_loads():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x12A06A0, 0x12A06A8, ".data"))
    qword(bv, 0x12A06A0, 0x123456789ABCDEF0)
    il = FakeMlil([], {})

    assert valorant.plan_global_constant_slots(bv, il) == [{
        "slot_addr": 0x12A06A0,
        "type": valorant.CONST_SLOT_TYPE,
        "value": 0x123456789ABCDEF0,
        "resolved_addr": 0x56789ABCDEF0,
        "use_addr": 0,
    }]


def test_global_constant_profile_plans_expanded_qword_slot_range():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x12A01E0, 0x12A0E38, ".data"))
    qword(bv, 0x12A01E0, 0x1111222233334444)
    qword(bv, 0x12A0E30, 0xAAAABBBBCCCCDDDD)
    il = FakeMlil([], {})

    assert valorant.plan_global_constant_slots(bv, il) == [
        {
            "slot_addr": 0x12A01E0,
            "type": valorant.CONST_SLOT_TYPE,
            "value": 0x1111222233334444,
            "resolved_addr": 0x222233334444,
            "use_addr": 0,
        },
        {
            "slot_addr": 0x12A0E30,
            "type": valorant.CONST_SLOT_TYPE,
            "value": 0xAAAABBBBCCCCDDDD,
            "resolved_addr": 0xBBBBCCCCDDDD,
            "use_addr": 0,
        },
    ]


def test_global_constant_profile_plans_scalar_constant_blob_data_vars():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x11F5700, 0x11F5878, ".data"))
    qword(bv, 0x11F5774, 0x11223344)
    qword(bv, 0x11F575B, 0x6677)
    qword(bv, 0x11F5700, 0x88)
    dword_load = load(
        "MLIL",
        binary("MLIL_ADD", const("MLIL", 0x11F576F), const("MLIL", 5)),
        size=4,
        address=0x6C801C,
    )
    word_load = load("MLIL", const("MLIL", 0x11F575B), size=2, address=0x6C8020)
    byte_load = load("MLIL", const("MLIL", 0x11F5700), size=1, address=0x6C8024)
    il = FakeMlil([dword_load, word_load, byte_load], {})

    assert valorant.plan_global_constant_slots(bv, il) == [
        {
            "slot_addr": 0x11F5700,
            "type": "uint8_t const",
            "value": 0x88,
            "resolved_addr": 0x88,
            "use_addr": 0x6C8024,
        },
        {
            "slot_addr": 0x11F575B,
            "type": "uint16_t const",
            "value": 0x6677,
            "resolved_addr": 0x6677,
            "use_addr": 0x6C8020,
        },
        {
            "slot_addr": 0x11F5774,
            "type": "uint32_t const",
            "value": 0x11223344,
            "resolved_addr": 0x11223344,
            "use_addr": 0x6C801C,
        },
    ]


def test_global_constant_profile_skips_out_of_range_loads():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x1000, 0x1008, ".data"))
    qword(bv, 0x1000, 0x123456789ABCDEF0)
    il = FakeMlil([], {})

    assert valorant.plan_global_constant_slots(bv, il) == []


def test_llil_value_folding_uses_stack_spill_before_memory_read():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    sp = Var("sp", 1)
    x1 = Var("x1", 1)
    slot = binary("LLIL_ADD", reg(sp), const("LLIL", 0x120))
    spill = store(slot, const("LLIL", 0x59), instr_index=1)
    reload = load("LLIL", slot)
    reload.instr_index = 2
    ssa = FakeSSA({x1: set_reg(reload, instr_index=3)}, [spill])

    assert valorant._values(None, ssa, reg(x1)) == {0x59}


def test_branch_value_folding_correlates_phi_arms():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    a0 = Var("x1", 1)
    a1 = Var("x1", 2)
    a = Var("x1", 3)
    b0 = Var("x2", 1)
    b1 = Var("x2", 2)
    b = Var("x2", 3)
    dest = binary("LLIL_ADD", reg(a), reg(b))
    ssa = FakeSSA({
        a0: set_reg(const("LLIL", 1), 1),
        a1: set_reg(const("LLIL", 2), 2),
        a: phi(a0, a1, instr_index=3),
        b0: set_reg(const("LLIL", 10), 4),
        b1: set_reg(const("LLIL", 20), 5),
        b: phi(b0, b1, instr_index=6),
    })

    assert valorant._branch_values(None, ssa, dest) == {11, 22}


def test_value_folding_preserves_bindings_through_direct_phi_expr():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    selected = Var("x1", 1)
    bound = Var("x2", 1)
    ssa = FakeSSA({selected: set_reg(reg(bound), 1)})

    assert valorant._values(None, ssa, phi(selected), bindings={bound: 0x77}) == {0x77}


if __name__ == "__main__":
    test_branch_profile_resolves_main_two_target_jump()
    test_call_profile_accepts_text_target_without_existing_function()
    test_call_profile_follows_ssa_call_destination()
    test_call_profile_accepts_external_symbol_target()
    test_global_constant_profile_plans_qword_data_loads()
    test_global_constant_profile_plans_expanded_qword_slot_range()
    test_global_constant_profile_plans_scalar_constant_blob_data_vars()
    test_global_constant_profile_skips_out_of_range_loads()
    test_llil_value_folding_uses_stack_spill_before_memory_read()
    test_branch_value_folding_correlates_phi_arms()
    test_value_folding_preserves_bindings_through_direct_phi_expr()
