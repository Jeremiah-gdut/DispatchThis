import types
from importlib import import_module

import conftest  # noqa: F401
from binaryninja import (
    LowLevelILOperation,
    MediumLevelILOperation,
    RegisterValueType,
    SymbolType,
)


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
        self.operation = MediumLevelILOperation.__members__.get(
            op,
            LowLevelILOperation.__members__.get(op, Op(op)),
        )
        self.children = list(children)
        self.expr_index = Expr._next_index
        Expr._next_index += 1
        self.__dict__.update(attrs)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out

    @property
    def vars_read(self):
        if self.operation.name in ("MLIL_VAR_SSA", "MLIL_VAR_ALIASED"):
            return [self.src]
        if self.operation.name == "MLIL_VAR_PHI":
            return list(self.src)
        return [
            variable
            for child in self.children
            for variable in child.vars_read
        ]


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

    def is_offset_executable(self, addr):
        return addr in self.valid_offsets and any(
            start <= addr < end and name == ".text"
            for start, end, name in self.sections
        )

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


class CorrelatedEdge:
    def __init__(self, source, target):
        self.source = source
        self.target = target


class CorrelatedBlock:
    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.incoming_edges = []
        self.outgoing_edges = []


class CorrelatedIl:
    def __init__(self, instructions, ssa_defs=None):
        self.instructions = list(instructions)
        self._by_index = {instruction.instr_index: instruction for instruction in self.instructions}
        self.ssa_defs = ssa_defs or {}

    def __getitem__(self, index):
        return self._by_index[index]

    def get_ssa_var_definition(self, var):
        return self.ssa_defs.get(var)


class IntType:
    width = 4
    const = False


class MutablePointerType:
    width = 8
    const = False


def const(op, value):
    return Expr(f"{op}_CONST", constant=value)


def reg(var):
    return Expr("LLIL_REG_SSA", src=var)


def var(name):
    return Expr("MLIL_VAR", src=name)


def var_ssa(name):
    return Expr("MLIL_VAR_SSA", src=name)


def set_reg(src, instr_index=0, block=None):
    return Expr(
        "LLIL_SET_REG_SSA",
        [src],
        src=src,
        instr_index=instr_index,
        il_basic_block=block,
    )


def set_var(dest, src, instr_index, address=0x4010):
    return Expr("MLIL_SET_VAR", [src], dest=dest, src=src, instr_index=instr_index, address=address)


def set_var_ssa(src, instr_index, address=0x4010):
    return Expr("MLIL_SET_VAR_SSA", [src], src=src, instr_index=instr_index, address=address)


def phi(*src, instr_index=0, block=None):
    return Expr(
        "LLIL_REG_PHI",
        src=list(src),
        instr_index=instr_index,
        il_basic_block=block,
    )


def unary(op, src):
    return Expr(op, [src], src=src)


def binary(op, left, right):
    return Expr(op, [left, right], left=left, right=right)


def load(op, src, size=8, address=0x4010):
    return Expr(f"{op}_LOAD", [src], src=src, size=size, address=address)


def store(dest, src, instr_index, src_memory=None, dest_memory=None, size=8):
    return Expr(
        "LLIL_STORE_SSA",
        [dest, src],
        dest=dest,
        src=src,
        instr_index=instr_index,
        src_memory=src_memory,
        dest_memory=dest_memory,
        size=size,
    )


def stack_address(expr, offset):
    expr.possible_values = types.SimpleNamespace(
        type=RegisterValueType.StackFrameOffset,
        offset=offset,
    )
    return expr


def qword(bv, addr, value):
    bv.memory[addr] = value & 0xFFFFFFFFFFFFFFFF


def correlated_store_fixture(
    reverse=True,
    mutable_pointer=False,
    impure_join_load=False,
):
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x5000, 0x6000, ".bss"))
    data_vars = {
        0x5100: types.SimpleNamespace(type=IntType()),
        0x5200: types.SimpleNamespace(type=IntType()),
        0x5300: types.SimpleNamespace(type=IntType()),
    }
    if mutable_pointer:
        bv.sections.append((0x6100, 0x6108, ".data"))
        data_vars[0x6100] = types.SimpleNamespace(type=MutablePointerType())
        qword(bv, 0x6100, 0x5200)
    bv.get_data_var_at = data_vars.get

    ssa_true = CorrelatedBlock(10, 11)
    ssa_false = CorrelatedBlock(20, 21)
    ssa_join = CorrelatedBlock(30, 31)
    true_edge = CorrelatedEdge(ssa_true, ssa_join)
    false_edge = CorrelatedEdge(ssa_false, ssa_join)
    ssa_true.outgoing_edges = [true_edge]
    ssa_false.outgoing_edges = [false_edge]
    ssa_join.incoming_edges = [true_edge, false_edge]

    dest_true = Var("dest", 1)
    dest_false = Var("dest", 2)
    dest_phi = Var("dest", 3)
    src_true = Var("src", 1)
    src_false = Var("src", 2)
    src_phi = Var("src", 3)
    src_value = Var("value", 1)

    dest_true_source = const("MLIL", 0x5200)
    if mutable_pointer:
        dest_true_source = load("MLIL", const("MLIL", 0x6100), size=8, address=0x4010)
    dest_true_def = set_var_ssa(dest_true_source, instr_index=1, address=0x4010)
    dest_false_def = set_var_ssa(const("MLIL", 0x5100), instr_index=2, address=0x4020)
    src_true_def = set_var_ssa(const("MLIL", 0x5100), instr_index=3, address=0x4010)
    src_false_def = set_var_ssa(
        const("MLIL", 0x5200 if reverse else 0x5300),
        instr_index=4,
        address=0x4020,
    )
    for definition, block in (
        (dest_true_def, ssa_true),
        (dest_false_def, ssa_false),
        (src_true_def, ssa_true),
        (src_false_def, ssa_false),
    ):
        definition.il_basic_block = block

    dest_phi_def = Expr("MLIL_VAR_PHI", src=[dest_true, dest_false], instr_index=5, address=0x5000)
    src_phi_def = Expr("MLIL_VAR_PHI", src=[src_true, src_false], instr_index=6, address=0x5000)
    dest_expr = Expr("MLIL_VAR_SSA", src=dest_phi)
    src_addr_expr = Expr("MLIL_VAR_SSA", src=src_phi)
    source_load = Expr(
        "MLIL_LOAD_SSA",
        [src_addr_expr],
        src=src_addr_expr,
        size=4,
        address=0x5000,
    )
    src_value_def = set_var_ssa(source_load, instr_index=7, address=0x5000)
    store_ssa = Expr(
        "MLIL_STORE_SSA",
        [dest_expr, Expr("MLIL_VAR_SSA", src=src_value)],
        dest=dest_expr,
        src=Expr("MLIL_VAR_SSA", src=src_value),
        size=4,
        instr_index=30,
        address=0x5000,
    )
    store_ssa.il_basic_block = ssa_join
    ssa_true_goto = Expr("MLIL_GOTO", instr_index=10, address=0x4014)
    ssa_false_goto = Expr("MLIL_GOTO", instr_index=20, address=0x4024)
    ssa_true_goto.il_basic_block = ssa_true
    ssa_false_goto.il_basic_block = ssa_false
    ssa = CorrelatedIl(
        [ssa_true_goto, ssa_false_goto, store_ssa],
        {
            dest_true: dest_true_def,
            dest_false: dest_false_def,
            dest_phi: dest_phi_def,
            src_true: src_true_def,
            src_false: src_false_def,
            src_phi: src_phi_def,
            src_value: src_value_def,
        },
    )

    non_ssa_join = CorrelatedBlock(40, 42)
    pure_source = (
        load("MLIL", const("MLIL", 0x5100), size=4)
        if impure_join_load
        else const("MLIL", 0)
    )
    pure = set_var("tmp", pure_source, instr_index=40, address=0x5000)
    pure.il_basic_block = non_ssa_join
    store = Expr("MLIL_STORE", dest=var("dest"), src=var("src"), size=4, instr_index=41, address=0x6000)
    store.il_basic_block = non_ssa_join
    true_goto = Expr("MLIL_GOTO", instr_index=50, address=0x5014)
    false_goto = Expr("MLIL_GOTO", instr_index=51, address=0x5024)
    il = CorrelatedIl([pure, store, true_goto, false_goto])
    il.ssa_form = ssa
    store_ssa.non_ssa_form = store
    ssa_true_goto.non_ssa_form = true_goto
    ssa_false_goto.non_ssa_form = false_goto
    func = types.SimpleNamespace(start=valorant.MAIN_START)
    return valorant, bv, func, il, store, true_goto, false_goto


def encoded_blob(plaintext, key):
    out = bytearray(key)
    previous = 0
    for index, plain in enumerate(plaintext):
        key_index = index % len(key)
        key_byte = key[key_index]
        decoded = plain ^ key_byte
        if ((key_index * key_byte) & 1) == 0:
            encoded = (((decoded ^ ((~key_byte) & 0xFF)) - previous) & 0xFF)
        else:
            encoded = ((((-decoded) & 0xFF) ^ key_byte) + previous) & 0xFF
        out.append(encoded)
        previous = plain
    return bytes(out)


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
        "jump_il": jump,
    }]
    assert valorant.resolve_branch_gadget(bv, il, {0x6C5F6C: (0x3000, 0xDEAD)}) == [{
        "source": 0x6C5F6C,
        "dest_expr_index": jump.dest.expr_index,
        "targets": (0x3000, 0x4000),
        "jump_il": jump,
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
    }]


def test_branch_profile_rejects_partly_invalid_cached_targets():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.valid_offsets.add(0x3000)
    bv.sections.append((0x3000, 0x3010, ".text"))
    target = Var("x8", 1)
    jump = Expr("LLIL_JUMP", [reg(target)], dest=reg(target), address=0x7000)
    jump.ssa_form = jump
    il = FakeLlil([[jump]])
    il.ssa_form = FakeSSA({})

    assert valorant.resolve_branch_gadget(bv, il, {0x7000: (0x3000, 0xDEAD)}) == []


def test_branch_profile_does_not_hide_current_invalid_path_with_cache(monkeypatch):
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.valid_offsets.add(0x3000)
    bv.sections.append((0x3000, 0x3010, ".text"))
    target = Var("x8", 1)
    jump = Expr("LLIL_JUMP", [reg(target)], dest=reg(target), address=0x7000)
    jump.ssa_form = jump
    il = FakeLlil([[jump]])
    il.ssa_form = FakeSSA({})
    monkeypatch.setattr(valorant, "_branch_values", lambda *_args: {0x3000, 0xDEAD})

    assert valorant.resolve_branch_gadget(bv, il, {0x7000: (0x3000,)}) == []


def test_peel_ssa_value_uses_binary_ninja_ssa_field_write_name():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    value = Expr("MLIL_CONST", constant=0x42)
    definition = Expr("MLIL_SET_VAR_SSA_FIELD", [value], src=value)

    assert valorant._peel_ssa_value(None, definition) is value


def test_call_profile_rejects_external_symbol_without_code_evidence():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    target = 0x8956BB63505153E0
    bv = FakeBv()
    bv.valid_offsets.add(target)
    bv.sections.append((target, target + 8, ".data"))
    bv.symbols[target] = types.SimpleNamespace(
        name="fork",
        type=SymbolType.ExternalSymbol,
    )

    decode_def = set_var("target", const("MLIL", target), instr_index=12, address=0x3200)
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4200)
    il = FakeMlil([decode_def, call_il], {"target": [decode_def]})

    assert valorant.resolve_call_gadget(bv, il) == []


def test_call_profile_rejects_partly_valid_complete_value_set():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.valid_offsets.add(0x5000)
    bv.sections.append((0x5000, 0x5100, ".text"))

    good = set_var("target", const("MLIL", 0x5000), instr_index=14)
    unknown = set_var("target", const("MLIL", 0xDEAD), instr_index=15)
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4400)
    il = FakeMlil([good, unknown, call_il], {"target": [good, unknown]})

    assert valorant.resolve_call_gadget(bv, il) == []


def test_call_profile_does_not_fallback_when_ssa_value_is_unknown():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.valid_offsets.add(0x5000)
    bv.sections.append((0x5000, 0x5100, ".text"))

    decode_def = set_var("target", const("MLIL", 0x5000), instr_index=16)
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4500)
    call_il.ssa_form = Expr(
        "MLIL_CALL_SSA",
        [var_ssa("target#1")],
        dest=var_ssa("target#1"),
        address=0x4500,
    )
    il = FakeMlil([decode_def, call_il], {"target": [decode_def]})
    il.ssa_form = FakeMlil([], {})

    assert valorant.resolve_call_gadget(bv, il) == []


def test_call_profile_rejects_data_symbol_target():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.symbols[0] = types.SimpleNamespace(
        name="__elf_header",
        type=SymbolType.DataSymbol,
    )

    decode_def = set_var("target", const("MLIL", 0), instr_index=13, address=0x3300)
    call_il = Expr("MLIL_CALL", [var("target")], dest=var("target"), address=0x4300)
    il = FakeMlil([decode_def, call_il], {"target": [decode_def]})

    assert valorant.resolve_call_gadget(bv, il) == []


def test_global_constant_profile_plans_qword_data_loads():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x12A06A0, 0x12A06A8, ".data"))
    qword(bv, 0x12A06A0, 0x123456789ABCDEF0)
    il = FakeMlil([], {})

    assert valorant.plan_global_constant_slots(bv, il) == [{
        "slot_addr": 0x12A06A0,
        "type": "uint64_t const",
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
            "type": "uint64_t const",
        },
        {
            "slot_addr": 0x12A0E30,
            "type": "uint64_t const",
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
        },
        {
            "slot_addr": 0x11F575B,
            "type": "uint16_t const",
        },
        {
            "slot_addr": 0x11F5774,
            "type": "uint32_t const",
        },
    ]


def test_global_constant_profile_plans_pre_blob_dword_constants():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.append((0x11F5678, 0x11F56A8, ".data"))
    qword(bv, 0x11F5678, 0xF228619E)
    qword(bv, 0x11F56A4, 0x4518F9EC)
    first_load = load("MLIL", const("MLIL", 0x11F5678), size=4, address=0x6C4D6C)
    last_load = load("MLIL", const("MLIL", 0x11F56A4), size=4, address=0x6C4FA0)
    il = FakeMlil([first_load, last_load], {})

    assert valorant.plan_global_constant_slots(bv, il) == [
        {
            "slot_addr": 0x11F5678,
            "type": "uint32_t const",
        },
        {
            "slot_addr": 0x11F56A4,
            "type": "uint32_t const",
        },
    ]


def test_global_constant_profile_plans_verified_path_pointer_slot_only():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    bv = FakeBv()
    bv.sections.extend([
        (0x11F5658, 0x11F5668, ".data"),
        (0x29D834, 0x29D844, ".rodata"),
    ])
    qword(bv, 0x11F5658, 0x29D834)
    qword(bv, 0x11F5660, 0x29D87E)

    assert valorant.plan_global_constant_slots(bv, FakeMlil([], {})) == [{
        "slot_addr": 0x11F5658,
        "type": "char const* const",
    }]


def test_correlated_store_profile_plans_arm_local_writes():
    valorant, bv, func, il, store_il, true_goto, false_goto = correlated_store_fixture()

    assert valorant.plan_correlated_store_rewrites(bv, func, il) == [{
        "store": store_il,
        "size": 4,
        "arms": (
            {"goto": true_goto, "dest": 0x5200, "src": 0x5100},
            {"goto": false_goto, "dest": 0x5100, "src": 0x5200},
        ),
    }]


def test_correlated_store_profile_rejects_non_swapping_phi_arms():
    valorant, bv, func, il, _store_il, _true_goto, _false_goto = correlated_store_fixture(reverse=False)

    assert valorant.plan_correlated_store_rewrites(bv, func, il) == []


def test_correlated_store_profile_rejects_writable_pointer_load():
    valorant, bv, func, il, _store_il, _true_goto, _false_goto = correlated_store_fixture(
        mutable_pointer=True
    )

    assert valorant.plan_correlated_store_rewrites(bv, func, il) == []


def test_correlated_store_profile_rejects_join_prefix_load():
    valorant, bv, func, il, *_rest = correlated_store_fixture(impure_join_load=True)

    assert valorant.plan_correlated_store_rewrites(bv, func, il) == []


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
    slot = stack_address(binary("LLIL_ADD", reg(sp), const("LLIL", 0x120)), -0x120)
    spill = store(slot, const("LLIL", 0x59), 1, src_memory=0, dest_memory=1)
    reload = Expr(
        "LLIL_LOAD_SSA",
        [slot],
        src=slot,
        size=8,
        instr_index=2,
        src_memory=1,
    )
    ssa = FakeSSA({x1: set_reg(reload, instr_index=3)}, [spill], {1: spill})

    assert valorant._values(None, ssa, reg(x1)) == {0x59}


def test_value_folding_requires_every_phi_arm_and_honors_struct_offset():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    unknown = Expr("LLIL_UNIMPL")

    assert valorant._values(None, FakeSSA({}), phi(const("LLIL", 7), unknown)) is None

    bv = FakeBv()
    qword(bv, 0x1010, 0x1234)
    load_struct = Expr(
        "MLIL_LOAD_STRUCT",
        [const("MLIL", 0x1000)],
        src=const("MLIL", 0x1000),
        offset=0x10,
        size=8,
    )

    assert valorant._values(bv, FakeMlil([], {}), load_struct) == {0x1234}


def test_value_folding_honors_cast_and_arithmetic_widths():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    source = const("LLIL", 0xFF)
    source.size = 1
    sx = unary("LLIL_SX", source)
    sx.size = 8
    low = unary("MLIL_LOW_PART", const("MLIL", 0x1234))
    low.size = 1
    add = binary("LLIL_ADD", const("LLIL", 0xFF), const("LLIL", 1))
    add.size = 1

    assert valorant._values(None, FakeSSA({}), sx) == {valorant.U64}
    assert valorant._values(None, FakeSSA({}), low) == {0x34}
    assert valorant._values(None, FakeSSA({}), add) == {0}


def test_target_validation_does_not_choose_executable_u48_alias():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    high = 0xFFFF000000003000

    assert valorant._validated_targets({high}, lambda target: target == 0x3000) is None


def test_branch_value_folding_correlates_phi_arms():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    a0 = Var("x1", 1)
    a1 = Var("x1", 2)
    a = Var("x1", 3)
    b0 = Var("x2", 1)
    b1 = Var("x2", 2)
    b = Var("x2", 3)
    dest = binary("LLIL_ADD", reg(a), reg(b))
    left = types.SimpleNamespace(start=10)
    right = types.SimpleNamespace(start=20)
    join = types.SimpleNamespace(
        start=30,
        incoming_edges=(
            types.SimpleNamespace(source=left),
            types.SimpleNamespace(source=right),
        ),
    )
    ssa = FakeSSA({
        a0: set_reg(const("LLIL", 1), 1, left),
        a1: set_reg(const("LLIL", 2), 2, right),
        a: phi(a0, a1, instr_index=3, block=join),
        b0: set_reg(const("LLIL", 10), 4, left),
        b1: set_reg(const("LLIL", 20), 5, right),
        b: phi(b1, b0, instr_index=6, block=join),
    })

    assert valorant._branch_values(None, ssa, dest) == {11, 22}


def test_value_folding_preserves_bindings_through_direct_phi_expr():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    selected = Var("x1", 1)
    bound = Var("x2", 1)
    ssa = FakeSSA({selected: set_reg(reg(bound), 1)})

    assert valorant._values(None, ssa, phi(selected), bindings={bound: 0x77}) == {0x77}


def test_binding_lookup_does_not_merge_distinct_same_named_variables():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")

    class NamedVariable:
        def __eq__(self, other):
            return self is other

        __hash__ = object.__hash__

        def __str__(self):
            return "state"

    first = NamedVariable()
    second = NamedVariable()

    bindings = {first: 0x77, str(first): 0x77}

    assert valorant._bound_value(bindings, second) is None


def test_string_decoder_stays_profile_local():
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    blob = encoded_blob(b"vanguard", b"k3y!")
    bv = types.SimpleNamespace(read=lambda _addr, size: blob[:size])

    assert valorant._decode_string_blob(
        bv,
        0x7000,
        {"key_modulus": 4, "length": 8},
    ) == b"vanguard"


def test_string_recognizers_reject_ambiguous_specs(monkeypatch):
    valorant = import_module("plugins.DispatchThis.profiles.valorant_2_6")
    il = object()
    monkeypatch.setattr(valorant, "_has_done_flag_store", lambda _il: True)
    monkeypatch.setattr(valorant, "_has_byte_crypto_store", lambda _il: True)
    monkeypatch.setattr(valorant, "_rem_moduli", lambda _il: {3})
    monkeypatch.setattr(valorant, "_cmp_ne_constants", lambda _il: {8, 9})

    assert valorant._recognize_rem_loop_string_decrypt(il) is None

    func = types.SimpleNamespace(mlil=il)
    monkeypatch.setattr(valorant, "_parameters", lambda *_args: ("dst", "src"))
    monkeypatch.setattr(
        valorant,
        "_recognize_rem_loop_string_decrypt",
        lambda _il: {"key_modulus": 3, "length": 5},
    )
    monkeypatch.setattr(
        valorant,
        "_recognize_index0_loop_string_decrypt",
        lambda _il: {"key_modulus": 4, "length": 5},
    )
    monkeypatch.setattr(valorant, "_recognize_unrolled_string_decrypt", lambda _il: None)

    assert valorant._recognize_string_decrypt_function(func) is None


if __name__ == "__main__":
    test_branch_profile_resolves_main_two_target_jump()
    test_branch_profile_rejects_partly_invalid_cached_targets()
    test_call_profile_accepts_text_target_without_existing_function()
    test_call_profile_follows_ssa_call_destination()
    test_call_profile_rejects_external_symbol_without_code_evidence()
    test_call_profile_rejects_partly_valid_complete_value_set()
    test_call_profile_does_not_fallback_when_ssa_value_is_unknown()
    test_call_profile_rejects_data_symbol_target()
    test_global_constant_profile_plans_qword_data_loads()
    test_global_constant_profile_plans_expanded_qword_slot_range()
    test_global_constant_profile_plans_verified_path_pointer_slot_only()
    test_global_constant_profile_plans_scalar_constant_blob_data_vars()
    test_global_constant_profile_plans_pre_blob_dword_constants()
    test_correlated_store_profile_plans_arm_local_writes()
    test_correlated_store_profile_rejects_non_swapping_phi_arms()
    test_correlated_store_profile_rejects_writable_pointer_load()
    test_global_constant_profile_skips_out_of_range_loads()
    test_llil_value_folding_uses_stack_spill_before_memory_read()
    test_value_folding_requires_every_phi_arm_and_honors_struct_offset()
    test_branch_value_folding_correlates_phi_arms()
    test_value_folding_preserves_bindings_through_direct_phi_expr()
    test_string_decoder_stays_profile_local()
