import types
from importlib import import_module

import conftest  # noqa: F401


driver = import_module("plugins.DispatchThis.profiles.driver_2_6")


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
        self.address = attrs.pop("address", 0x1000 + self.expr_index)
        self.instr_index = attrs.pop("instr_index", None)
        self.__dict__.update(attrs)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out


class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target


class Block:
    def __init__(self, start):
        self.start = start
        self.end = start
        self.instructions = []
        self.incoming_edges = []
        self.outgoing_edges = []

    def add(self, expr):
        expr.instr_index = self.end
        expr.il_basic_block = self
        self.instructions.append(expr)
        self.end += 1
        return expr

    def __iter__(self):
        return iter(self.instructions)


class FakeMlil:
    def __init__(self, blocks):
        self.basic_blocks = blocks
        self.instructions = []
        self.by_index = {}
        self.defs = {}
        for block in blocks:
            for ins in block:
                self.instructions.append(ins)
                self.by_index[ins.instr_index] = ins
                if ins.operation.name == "MLIL_SET_VAR":
                    self.defs.setdefault(ins.dest, []).append(ins)

    def __getitem__(self, index):
        return self.by_index[index]

    def get_var_definitions(self, var):
        return self.defs.get(var, [])


class FakeBv:
    def __init__(self):
        self.memory = {}
        self.functions = {}
        self.data_vars = {}
        self.sections = {}

    def read(self, addr, size):
        return self.memory.get(addr, b"")[:size]

    def get_function_at(self, addr):
        return self.functions.get(addr)

    def get_data_var_at(self, addr):
        return self.data_vars.get(addr)

    def get_sections_at(self, addr):
        return self.sections.get(addr, [])


class DataVar:
    def __init__(self, type_name):
        self.type = type_name


class Section:
    def __init__(self, name):
        self.name = name


class FakeFunc:
    def __init__(self, start, il):
        self.start = start
        self.medium_level_il = il


def const(value, size=4):
    return Expr("MLIL_CONST", constant=value, size=size)


def var(name):
    return Expr("MLIL_VAR", src=name)


def addr_of(name):
    return Expr("MLIL_ADDRESS_OF", src=name)


def set_var(dest, src, address=0x1000):
    return Expr("MLIL_SET_VAR", [src], dest=dest, src=src, vars_written={dest}, address=address)


def binary(op, left, right):
    return Expr(op, [left, right], left=left, right=right)


def load(src, size=8, address=0x1000):
    return Expr("MLIL_LOAD", [src], src=src, size=size, address=address)


def store(dest, src):
    return Expr("MLIL_STORE", [dest, src], dest=dest, src=src)


def goto():
    return Expr("MLIL_GOTO")


def if_eq(name, token, true_idx, false_idx):
    cond = Expr("MLIL_CMP_E", [var(name), const(token)], left=var(name), right=const(token))
    return Expr("MLIL_IF", [cond], condition=cond, true=true_idx, false=false_idx)


def if_cond(true_idx, false_idx):
    cond = var("cond")
    return Expr("MLIL_IF", [cond], condition=cond, true=true_idx, false=false_idx)


def call(dest, params):
    return Expr("MLIL_CALL", [dest, *params], dest=dest, params=params, address=0x5000)


def link(source, target):
    edge = Edge(source, target)
    source.outgoing_edges.append(edge)
    target.incoming_edges.append(edge)


def build_driver_shape():
    entry = Block(0)
    row_a = Block(10)
    row_b = Block(20)
    row_c = Block(30)
    real_a = Block(40)
    true_tail = Block(50)
    false_tail = Block(60)
    store_tail = Block(70)
    real_b = Block(80)
    real_c = Block(90)
    loop = Block(100)

    entry.add(set_var("state", const(0x1111)))
    entry.add(set_var("state_ptr", addr_of("state")))
    entry_jump = entry.add(goto())

    row_a.add(set_var("x0", var("state")))
    row_a.add(set_var("temp0", var("x0")))
    row_a.add(if_eq("temp0", 0x1111, real_a.start, row_b.start))

    row_b.add(set_var("x1", var("state")))
    row_b.add(set_var("temp1", var("x1")))
    row_b.add(if_eq("temp1", 0x2222, real_b.start, row_c.start))

    row_c.add(set_var("x2", var("state")))
    row_c.add(set_var("temp2", var("x2")))
    row_c.add(if_eq("temp2", 0x3333, real_c.start, loop.start))

    branch = real_a.add(if_cond(true_tail.start, false_tail.start))
    true_tail.add(set_var("next", const(0x2222)))
    true_tail.add(goto())
    false_tail.add(set_var("next", const(0x3333)))
    false_tail.add(goto())
    store_tail.add(set_var("ptr", var("state_ptr")))
    store_tail.add(store(var("ptr"), var("next")))
    store_tail.add(goto())

    real_b.add(set_var("ptr_b", var("state_ptr")))
    real_b.add(store(var("ptr_b"), const(0x3333)))
    real_b_jump = real_b.add(goto())

    real_c.add(Expr("MLIL_RET"))
    loop.add(goto())

    link(entry, row_a)
    link(row_a, real_a)
    link(row_a, row_b)
    link(row_b, real_b)
    link(row_b, row_c)
    link(row_c, real_c)
    link(row_c, loop)
    link(real_a, true_tail)
    link(real_a, false_tail)
    link(true_tail, store_tail)
    link(false_tail, store_tail)
    link(store_tail, loop)
    link(real_b, loop)
    link(loop, row_a)

    blocks = [entry, row_a, row_b, row_c, real_a, true_tail, false_tail, store_tail, real_b, real_c, loop]
    return FakeMlil(blocks), entry_jump, branch, real_b_jump, real_b, real_c


def one_block(*instructions):
    block = Block(0)
    for ins in instructions:
        block.add(ins)
    return FakeMlil([block])


def driver_blob(plaintext, key):
    out = bytearray(key)
    previous = 0
    for index, plain in enumerate(plaintext):
        key_index = index % len(key)
        key_byte = key[key_index]
        decoded = plain ^ key_byte
        if ((key_index * key_byte) & 1) == 0:
            cipher = (((decoded ^ ((~key_byte) & 0xFF)) - previous) & 0xFF)
        else:
            cipher = ((((-decoded) & 0xFF) ^ key_byte) + previous) & 0xFF
        out.append(cipher)
        previous = plain
    return bytes(out)


def decrypt_callee(start, key_modulus, length):
    next_i = set_var("next_i", binary("MLIL_ADD", var("i"), const(1)))
    divu = set_var("mod_i", binary("MLIL_DIVU", var("i"), const(key_modulus)))
    parity = set_var("parity", binary("MLIL_AND", binary("MLIL_MUL", var("mod_i"), var("key")), const(1)))
    mixed = set_var("mixed", binary("MLIL_XOR", var("cipher"), var("key")))
    key_load = set_var("key", Expr("MLIL_LOAD", [var("src")], src=var("src"), size=1))
    payload_load = set_var("cipher", Expr("MLIL_LOAD", [var("payload")], src=var("payload"), size=1))
    byte_store = store(var("dst"), var("mixed"))
    byte_store.size = 1
    cond = Expr("MLIL_CMP_E", [var("next_i"), const(length)], left=var("next_i"), right=const(length))
    done_if = Expr("MLIL_IF", [cond], condition=cond, true=0, false=0)
    return FakeFunc(start, one_block(
        next_i,
        divu,
        parity,
        key_load,
        payload_load,
        mixed,
        byte_store,
        done_if,
    ))


def counter_loop_callee(start, key_modulus, length):
    next_i = set_var("next_i", binary("MLIL_ADD", var("i"), const(1)))
    divu = set_var("mod_i", binary("MLIL_DIVU", var("i"), const(key_modulus)))
    cond = Expr("MLIL_CMP_E", [var("next_i"), const(length)], left=var("next_i"), right=const(length))
    done_if = Expr("MLIL_IF", [cond], condition=cond, true=0, false=0)
    return FakeFunc(start, one_block(next_i, divu, done_if))


def test_driver_deflatten_hook_handles_stack_state_stores():
    il, entry_jump, branch, real_b_jump, real_b, real_c = build_driver_shape()
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    entry_plan = next(plan for plan in plans if plan.get("entry"))
    assert entry_plan["jump"] is entry_jump
    assert entry_plan["target_bb"].start == 40
    assert entry_plan["state_tokens"] == {(0x1111, 4)}

    conditional = next(plan for plan in plans if plan["kind"] == "if_else")
    assert conditional["if_il"] is branch
    assert conditional["true_target"].start == real_b.start
    assert conditional["false_target"].start == real_c.start
    assert conditional["state_tokens"] == {(0x2222, 4), (0x3333, 4)}

    uncond = next(plan for plan in plans if plan.get("jump") is real_b_jump)
    assert uncond["target_bb"].start == real_c.start
    assert uncond["state_tokens"] == {(0x3333, 4)}


def test_driver_deflatten_hook_skips_conditional_tail_with_real_store():
    il, _entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    true_tail = next(bb for bb in il.basic_blocks if bb.start == 50)
    tail_goto = true_tail.instructions[-1]
    real_store = store(var("global_ptr"), const(0x99))
    real_store.instr_index = tail_goto.instr_index
    real_store.il_basic_block = true_tail
    tail_goto.instr_index += 1
    true_tail.instructions.insert(-1, real_store)
    true_tail.end += 1
    il.instructions.append(real_store)
    il.by_index[real_store.instr_index] = real_store
    il.by_index[tail_goto.instr_index] = tail_goto
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_global_constant_hook_plans_driver_blob_base_slot():
    bv = FakeBv()
    slot = 0x2987E0
    value = 0x6DE0D0EBF3F9FF28
    bv.data_vars[slot] = DataVar("void*")
    bv.sections[slot] = [Section(".data")]
    bv.memory[slot] = value.to_bytes(8, "little")
    il = one_block(set_var("x9", load(const(slot), address=0x30A40), address=0x30A40))

    assert driver.plan_global_constant_slots(bv, il) == [{
        "slot_addr": slot,
        "type": "void const* const",
        "value": value,
        "resolved_addr": value & 0xFFFFFFFFFFFF,
        "use_addr": 0x30A40,
    }]


def test_driver_string_decrypt_hook_recovers_clone_calls():
    bv = FakeBv()
    callee = decrypt_callee(0x5E334, 3, 5)
    bv.functions[callee.start] = callee
    bv.memory[0x7000] = driver_blob(b"drv\x00!", b"aB?")
    caller = FakeFunc(
        0x36D10,
        one_block(call(const(callee.start), [const(0x6000), const(0x7000)])),
    )

    facts = driver.plan_string_decrypt_calls(bv, caller, caller.medium_level_il, {})

    assert facts == [{
        "call_addr": 0x5000,
        "src_addr": 0x7000,
        "dst_addr": 0x6000,
        "plaintext": b"drv\x00!",
    }]


def test_driver_string_decrypt_hook_rejects_counter_loop_callee():
    bv = FakeBv()
    callee = counter_loop_callee(0x5000, 3, 5)
    bv.functions[callee.start] = callee
    bv.memory[0x7000] = driver_blob(b"drv\x00!", b"aB?")
    caller = FakeFunc(
        0x36D10,
        one_block(call(const(callee.start), [const(0x6000), const(0x7000)])),
    )

    assert driver.plan_string_decrypt_calls(bv, caller, caller.medium_level_il, {}) == []


def test_driver_string_decrypt_hook_rejects_oversized_key_modulus():
    bv = FakeBv()
    callee = decrypt_callee(0x5000, 4097, 5)
    bv.functions[callee.start] = callee
    caller = FakeFunc(
        0x36D10,
        one_block(call(const(callee.start), [const(0x6000), const(0x7000)])),
    )

    assert driver.plan_string_decrypt_calls(bv, caller, caller.medium_level_il, {}) == []


if __name__ == "__main__":
    test_driver_deflatten_hook_handles_stack_state_stores()
    test_driver_deflatten_hook_skips_conditional_tail_with_real_store()
    test_driver_global_constant_hook_plans_driver_blob_base_slot()
    test_driver_string_decrypt_hook_recovers_clone_calls()
    test_driver_string_decrypt_hook_rejects_counter_loop_callee()
    test_driver_string_decrypt_hook_rejects_oversized_key_modulus()
