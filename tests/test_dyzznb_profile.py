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
from test_string_decrypt import (
    FakeBv as StringFakeBv,
    FakeFunc as StringFakeFunc,
    FakeMlil as StringFakeMlil,
    call as string_call,
    const as string_const,
    decrypt_callee,
    encoded_blob,
)


def test_branch_profile_reuses_default_planner():
    dyzznb = import_module("plugins.DispatchThis.profiles.dyzznb")

    assert dyzznb.resolve_branch_gadget is dyzznb.default.resolve_branch_gadget


def test_call_profile_returns_call_facts():
    dyzznb = import_module("plugins.DispatchThis.profiles.dyzznb")
    bv, il, call_il, decode_def = decoded_call_fixture()

    assert dyzznb.resolve_call_gadget(bv, il) == [{
        "call_il": call_il,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": decode_def,
        "cleanup_roots": {0},
        "cleanup_load_roots": {0},
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


def test_string_profile_returns_decrypt_facts():
    dyzznb = import_module("plugins.DispatchThis.profiles.dyzznb")
    bv = StringFakeBv()
    callee = decrypt_callee("glDrawElements")
    bv.functions[callee.start] = callee
    bv.session_data["dispatchthis_mlil_stable"][callee.start] = True
    bv.memory[0x7000] = encoded_blob("glDrawElements")
    caller = StringFakeFunc(0x1000)
    caller.mlil = StringFakeMlil(
        [string_call(string_const(callee.start), [string_const(0x6000), string_const(0x7000)])],
        caller,
    )

    assert dyzznb.plan_string_decrypt_calls(
        bv,
        caller,
        caller.mlil,
        bv.session_data["dispatchthis_mlil_stable"],
    ) == [{
        "call_addr": 0x5000,
        "src_addr": 0x7000,
        "dst_addr": 0x6000,
        "plaintext": b"glDrawElements",
    }]
