import types

from conftest import load_plugin_module


global_constants = load_plugin_module("plugins.DispatchThis.passes.medium.global_constants")

CONST_SLOT_TYPE = global_constants.CONST_SLOT_TYPE
plan_global_constant_slots = global_constants.plan_global_constant_slots


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    def __init__(self, op, **attrs):
        self.operation = Op(op)
        self.__dict__.update(attrs)


class DataVar:
    def __init__(self, type_name, refs=()):
        self.type = type_name
        self.code_refs = list(refs)


class Ref:
    def __init__(self, address, function=None):
        self.address = address
        self.function = function


class Section:
    def __init__(self, name):
        self.name = name


class FakeBv:
    def __init__(self):
        self.data_vars = {}
        self.memory = {}
        self.valid_offsets = set()
        self.sections = {}
        self.functions = []

    def get_data_var_at(self, addr):
        return self.data_vars.get(addr)

    def get_sections_at(self, addr):
        return self.sections.get(addr, [])

    def read(self, addr, size):
        value = self.memory.get(addr)
        if value is None or size != 8:
            return b""
        return value.to_bytes(8, "little")

    def is_valid_offset(self, addr):
        return addr in self.valid_offsets

    def get_functions_containing(self, addr):
        return [f for f in self.functions if f.start <= addr < f.end_addr]


class FakeFunc:
    def __init__(self, mlil, start=0x4000, end_addr=0x5000):
        self.mlil = mlil
        self.start = start
        self.end_addr = end_addr


class FakeMlil:
    def __init__(self, instructions, defs=None):
        self.instructions = instructions
        self._defs = defs or {}

    def get_var_definitions(self, var):
        return self._defs.get(var, [])


def const(value):
    return Expr("MLIL_CONST_PTR", constant=value)


def var(name):
    return Expr("MLIL_VAR", src=name)


def add(left, right):
    return Expr("MLIL_ADD", left=left, right=right)


def load(src, size=8, address=0x1000):
    return Expr("MLIL_LOAD", src=src, size=size, address=address)


def set_var(name, src, address=0x1000):
    return Expr("MLIL_SET_VAR", dest=name, src=src, address=address)


def store(dest, address=0x1000):
    return Expr("MLIL_STORE", dest=dest, address=address)


def call(dest, params=(), address=0x1000):
    return Expr("MLIL_CALL", dest=dest, params=list(params), address=address)


def test_global_constant_slot_is_planned_from_pointer_base_load():
    bv = FakeBv()
    bv.data_vars[0xA43D70] = DataVar("void*")
    bv.sections[0xA43D70] = [Section(".data")]
    bv.memory[0xA43D70] = 0x5F88806BDE3FE98C
    bv.valid_offsets.add(0xA49C30)

    slot_load = set_var("x10_41", load(const(0xA43D70), address=0x8E1260), address=0x8E1260)
    base_add = set_var("x10_42", add(var("x10_41"), const(-0x5F88806BDD9B4E30)), address=0x8E1278)
    value_load = load(add(var("x10_42"), const(0xD4)), address=0x8E127C)
    mlil = FakeMlil(
        [slot_load, base_add, value_load],
        {"x10_41": [slot_load], "x10_42": [base_add]},
    )

    assert plan_global_constant_slots(bv, mlil) == [
        {
            "slot_addr": 0xA43D70,
            "type": CONST_SLOT_TYPE,
        }
    ]


def test_global_constant_slot_is_skipped_when_known_refs_store_to_it():
    bv = FakeBv()
    ref_func = FakeFunc(None)
    bv.functions = [ref_func]
    bv.data_vars[0xA43D70] = DataVar("void*", [Ref(0x4010, ref_func)])
    bv.sections[0xA43D70] = [Section(".data")]
    bv.memory[0xA43D70] = 0x5F88806BDE3FE98C
    bv.valid_offsets.add(0xA49C30)

    slot_load = set_var("x10_41", load(const(0xA43D70), address=0x8E1260), address=0x8E1260)
    base_add = set_var("x10_42", add(var("x10_41"), const(-0x5F88806BDD9B4E30)), address=0x8E1278)
    value_load = load(add(var("x10_42"), const(0xD4)), address=0x8E127C)
    current_mlil = FakeMlil(
        [slot_load, base_add, value_load],
        {"x10_41": [slot_load], "x10_42": [base_add]},
    )
    ref_func.mlil = FakeMlil([store(const(0xA43D70), address=0x4020)])

    assert plan_global_constant_slots(bv, current_mlil) == []


def test_global_constant_slot_is_planned_from_call_argument_pointer():
    bv = FakeBv()
    bv.data_vars[0xA45660] = DataVar("void*")
    bv.sections[0xA45660] = [Section(".data")]
    bv.memory[0xA45660] = 0x4A6309F1F5DCFBC9
    bv.valid_offsets.add(0xA20C2A)

    slot_load = set_var("x9_38", load(const(0xA45660), address=0x925364), address=0x925364)
    base_add = set_var(
        "x1_1",
        add(var("x9_38"), const(-0x4A6309F1F53AEF9F)),
        address=0x925378,
    )
    use_call = call(const(0x9D4164), [const(0xA6A590), var("x1_1")], address=0x925388)
    mlil = FakeMlil(
        [slot_load, base_add, use_call],
        {"x9_38": [slot_load], "x1_1": [base_add]},
    )

    assert plan_global_constant_slots(bv, mlil) == [
        {
            "slot_addr": 0xA45660,
            "type": CONST_SLOT_TYPE,
        }
    ]


if __name__ == "__main__":
    test_global_constant_slot_is_planned_from_pointer_base_load()
    test_global_constant_slot_is_skipped_when_known_refs_store_to_it()
    test_global_constant_slot_is_planned_from_call_argument_pointer()
