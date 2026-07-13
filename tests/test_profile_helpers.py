from importlib import import_module

import pytest
from binaryninja import SymbolType


class Section:
    def __init__(self, name):
        self.name = name


class FakeBv:
    def __init__(self):
        self.memory = {}
        self.valid_offsets = set()
        self.sections = {}
        self.symbols = {}
        self.functions = {}
        self.executable_offsets = set()
        self.raise_on_read = set()

    def read(self, addr, size):
        if addr in self.raise_on_read:
            raise RuntimeError("invalid read")
        return self.memory.get(addr, b"")[:size]

    def is_valid_offset(self, addr):
        return addr in self.valid_offsets

    def is_offset_executable(self, addr):
        return addr in self.executable_offsets

    def get_sections_at(self, addr):
        return self.sections.get(addr, [])

    def get_symbol_at(self, addr):
        return self.symbols.get(addr)

    def get_function_at(self, addr):
        return self.functions.get(addr)


class CallIl:
    address = 0x4000


class JumpIl:
    def __init__(self, address=0x1000, dest_expr_index=7):
        self.address = address
        self.dest = type("Dest", (), {"expr_index": dest_expr_index})()


def test_helper_package_exposes_stable_module_imports():
    helpers = import_module("plugins.DispatchThis.helpers")

    assert helpers.llil is import_module("plugins.DispatchThis.helpers.llil")
    assert helpers.mlil is import_module("plugins.DispatchThis.helpers.mlil")
    assert helpers.memory is import_module("plugins.DispatchThis.helpers.memory")
    assert helpers.facts is import_module("plugins.DispatchThis.helpers.facts")


def test_memory_helpers_read_explicit_little_endian_widths():
    memory = import_module("plugins.DispatchThis.helpers.memory")
    bv = FakeBv()
    bv.memory[0x1000] = b"\x01\x02\x03\x04\x05\x06\x07\x08"

    assert memory.read_u8(bv, 0x1000) == 0x01
    assert memory.read_u16le(bv, 0x1000) == 0x0201
    assert memory.read_u32le(bv, 0x1000) == 0x04030201
    assert memory.read_u64le(bv, 0x1000) == 0x0807060504030201
    assert memory.read_qword_slot(bv, 0x1000) == 0x0807060504030201


def test_memory_helpers_return_none_for_short_or_invalid_reads():
    memory = import_module("plugins.DispatchThis.helpers.memory")
    bv = FakeBv()
    bv.memory[0x1000] = b"\x01\x02"
    bv.raise_on_read.add(0x2000)

    assert memory.read_u32le(bv, 0x1000) is None
    assert memory.read_u8(bv, 0x2000) is None
    assert memory.read_u16le(bv, 0x3000) is None

    with pytest.raises(ValueError, match="width"):
        memory.read_uint_le(bv, 0x1000, 0)


def test_memory_helpers_validate_addresses_targets_and_sections():
    memory = import_module("plugins.DispatchThis.helpers.memory")
    bv = FakeBv()
    symbol = type("Symbol", (), {"type": SymbolType.ImportedFunctionSymbol})()
    data_symbol = type("Symbol", (), {"type": SymbolType.DataSymbol})()
    func = object()
    bv.valid_offsets.update({0x1000, 0x2000, 0x3000, 0x4000, 0x5000})
    bv.executable_offsets.add(0x4000)
    bv.sections[0x1000] = [Section(".data")]
    bv.symbols[0x2000] = symbol
    bv.symbols[0x5000] = data_symbol
    bv.functions[0x3000] = func

    assert memory.is_mapped_address(bv, 0x1000)
    assert memory.is_known_callee(bv, 0x2000)
    assert memory.is_known_callee(bv, 0x3000)
    assert memory.is_executable_target(bv, 0x4000)
    assert memory.is_known_callee(bv, 0x4000)
    assert not memory.is_known_callee(bv, 0x1000)
    assert not memory.is_known_callee(bv, 0x5000)
    assert not memory.is_mapped_address(bv, 0x6000)
    assert memory.in_section(bv, 0x1000, ".data")
    assert memory.in_section(bv, 0x1000, (".rodata", ".data"))
    assert not memory.in_section(bv, 0x1000, ".text")


def test_fact_builders_return_existing_recovery_fact_shapes():
    facts = import_module("plugins.DispatchThis.helpers.facts")
    jump_il = JumpIl()
    call_il = CallIl()
    decode_def = object()

    assert facts.branch_fact(jump_il, [0x3000, 0x2000, 0x3000]) == {
        "source": 0x1000,
        "dest_expr_index": 7,
        "targets": (0x2000, 0x3000),
        "jump_il": jump_il,
    }
    assert facts.call_fact(call_il, 0x5000, decode_def=decode_def) == {
        "call_il": call_il,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": decode_def,
    }
    assert facts.global_constant_fact(0xA43D70, "uint8_t const* const") == {
        "slot_addr": 0xA43D70,
        "type": "uint8_t const* const",
    }
    assert facts.string_decrypt_fact(0x9000, 0xA000, 0xB000, bytearray(b"hello")) == {
        "call_addr": 0x9000,
        "src_addr": 0xA000,
        "dst_addr": 0xB000,
        "plaintext": b"hello",
    }


def test_fact_builders_reject_malformed_required_fields():
    facts = import_module("plugins.DispatchThis.helpers.facts")
    jump_il = JumpIl()

    with pytest.raises(facts.MalformedRecoveryFact, match="targets"):
        facts.branch_fact(jump_il, [])
    with pytest.raises(facts.MalformedRecoveryFact, match="jump_il"):
        facts.branch_fact(None, [0x2000])
    with pytest.raises(facts.MalformedRecoveryFact, match="call_il"):
        facts.call_fact(None, 0x5000)
    with pytest.raises(facts.MalformedRecoveryFact, match="call_addr"):
        facts.call_fact(object(), 0x5000)
    with pytest.raises(facts.MalformedRecoveryFact, match="slot_addr"):
        facts.global_constant_fact(None, "uint8_t const* const")
    with pytest.raises(facts.MalformedRecoveryFact, match="plaintext"):
        facts.string_decrypt_fact(0x9000, 0xA000, 0xB000, "hello")
    with pytest.raises(facts.MalformedRecoveryFact, match="source"):
        facts.branch_fact(JumpIl(address=-1), [0x2000])
    with pytest.raises(facts.MalformedRecoveryFact, match="dest_expr_index"):
        facts.branch_fact(JumpIl(dest_expr_index=-1), [0x2000])
    with pytest.raises(facts.MalformedRecoveryFact, match="targets"):
        facts.branch_fact(jump_il, [-1])
