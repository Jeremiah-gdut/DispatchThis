from importlib import import_module

from test_global_constants import (
    DataVar,
    FakeBv as GlobalFakeBv,
    FakeMlil as GlobalFakeMlil,
    Section,
    add as global_add,
    const as global_const,
    load as global_load,
    set_var as global_set_var,
    var as global_var,
)
from test_indirect_calls import decoded_call_fixture


def test_call_profile_returns_call_facts():
    dyzznb = import_module("plugins.DispatchThis.profiles.dyzznb")
    bv, il, call_il, decode_def = decoded_call_fixture()

    assert dyzznb.resolve_call_gadget(bv, il) == [{
        "call_il": call_il,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": decode_def,
    }]


def test_global_profile_returns_constant_slot_facts():
    dyzznb = import_module("plugins.DispatchThis.profiles.dyzznb")
    bv = GlobalFakeBv()
    bv.data_vars[0xA43D70] = DataVar("void*")
    bv.sections[0xA43D70] = [Section(".data")]
    bv.memory[0xA43D70] = 0x5F88806BDE3FE98C
    bv.valid_offsets.add(0xA49C30)

    slot_load = global_set_var("x10_41", global_load(global_const(0xA43D70), address=0x8E1260), address=0x8E1260)
    base_add = global_set_var(
        "x10_42",
        global_add(global_var("x10_41"), global_const(-0x5F88806BDD9B4E30)),
        address=0x8E1278,
    )
    value_load = global_load(global_add(global_var("x10_42"), global_const(0xD4)), address=0x8E127C)
    il = GlobalFakeMlil(
        [slot_load, base_add, value_load],
        {"x10_41": [slot_load], "x10_42": [base_add]},
    )

    assert dyzznb.plan_global_constant_slots(bv, il) == [{
        "slot_addr": 0xA43D70,
        "type": dyzznb.CONST_SLOT_TYPE,
    }]
