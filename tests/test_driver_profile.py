import types
from importlib import import_module

import conftest  # noqa: F401
import pytest
from binaryninja import MediumLevelILOperation, TypeClass


driver = import_module("plugins.DispatchThis.profiles.driver_2_6")


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    _next_index = 1

    def __init__(self, op, children=(), **attrs):
        self.operation = MediumLevelILOperation.__members__.get(op, Op(op))
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
        self.dominators = []

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
        self._set_dominators()

    def __getitem__(self, index):
        return self.by_index[index]

    def get_basic_block_at(self, index):
        instruction = self.by_index.get(index)
        return getattr(instruction, "il_basic_block", None)

    def _set_dominators(self):
        if not self.basic_blocks:
            return
        entry = self.basic_blocks[0]
        all_blocks = set(self.basic_blocks)
        dominators = {
            block: ({entry} if block is entry else set(all_blocks))
            for block in self.basic_blocks
        }
        changed = True
        while changed:
            changed = False
            for block in self.basic_blocks[1:]:
                predecessors = [
                    dominators[edge.source]
                    for edge in block.incoming_edges
                    if edge.source in dominators
                ]
                new = (
                    {block}
                    if not predecessors
                    else {block} | set.intersection(*(set(items) for items in predecessors))
                )
                if new != dominators[block]:
                    dominators[block] = new
                    changed = True
        for block, items in dominators.items():
            block.dominators = list(items)

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
        if type_name == "void*":
            self.type = types.SimpleNamespace(
                type_class=TypeClass.PointerTypeClass,
                width=8,
                target=types.SimpleNamespace(type_class=TypeClass.VoidTypeClass),
            )
        elif type_name == "int64_t":
            self.type = types.SimpleNamespace(
                type_class=TypeClass.IntegerTypeClass,
                width=8,
                signed=True,
            )
        else:
            raise ValueError(type_name)


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


def addr_of_field(name, offset=0):
    return Expr("MLIL_ADDRESS_OF_FIELD", src=name, offset=offset)


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


def if_compare(name, op, bound, true_idx, false_idx):
    cond = Expr(op, [var(name), const(bound)], left=var(name), right=const(bound))
    return Expr("MLIL_IF", [cond], condition=cond, true=true_idx, false=false_idx)


def if_cond(true_idx, false_idx):
    cond = var("cond")
    return Expr("MLIL_IF", [cond], condition=cond, true=true_idx, false=false_idx)


def call(dest, params, address=0x5000):
    return Expr("MLIL_CALL", [dest, *params], dest=dest, params=params, address=address)


def link(source, target):
    edge = Edge(source, target)
    source.outgoing_edges.append(edge)
    target.incoming_edges.append(edge)


def build_driver_shape(range_dispatch=False):
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
    row_a.add(
        if_compare("temp0", "MLIL_CMP_ULT", 0x1800, real_a.start, row_b.start)
        if range_dispatch
        else if_eq("temp0", 0x1111, real_a.start, row_b.start)
    )

    row_b.add(set_var("x1", var("state")))
    row_b.add(set_var("temp1", var("x1")))
    row_b.add(
        if_compare("temp1", "MLIL_CMP_ULT", 0x2800, real_b.start, row_c.start)
        if range_dispatch
        else if_eq("temp1", 0x2222, real_b.start, row_c.start)
    )

    row_c.add(set_var("x2", var("state")))
    row_c.add(set_var("temp2", var("x2")))
    row_c.add(
        if_compare("temp2", "MLIL_CMP_ULT", 0x3800, real_c.start, loop.start)
        if range_dispatch
        else if_eq("temp2", 0x3333, real_c.start, loop.start)
    )

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
    assert entry_plan["exit_jumps"] == (entry_jump,)
    assert entry_plan["target_bb"].start == 40
    assert entry_plan["state_token"] == (0x1111, 4)

    conditional = next(plan for plan in plans if plan["kind"] == "if_else")
    assert conditional["if_il"] is branch
    assert conditional["true_target"].start == real_b.start
    assert conditional["false_target"].start == real_c.start
    assert conditional["true_token"] == (0x2222, 4)
    assert conditional["false_token"] == (0x3333, 4)
    conditional_store = next(
        ins
        for ins in il.instructions
        if ins.operation.name == "MLIL_STORE" and ins.il_basic_block.start == 70
    )
    assert conditional["obsolete_state_writes"] == {conditional_store.instr_index}

    uncond = next(plan for plan in plans if real_b_jump in plan.get("exit_jumps", ()))
    assert uncond["target_bb"].start == real_c.start
    assert uncond["state_token"] == (0x3333, 4)


def test_driver_deflatten_rejects_a_narrow_state_store():
    il, _entry_jump, _branch, real_b_jump, real_b, _real_c = build_driver_shape()
    state_store = next(ins for ins in real_b if ins.operation == MediumLevelILOperation.MLIL_STORE)
    state_store.size = 2
    state_store.src.size = 2

    plans = driver.plan_deflatten_redirections(None, types.SimpleNamespace(start=0x36D10), il)

    assert real_b_jump not in {
        jump
        for plan in plans
        for jump in plan.get("exit_jumps", ())
    }


def test_driver_dispatcher_boundary_rejects_a_different_token_width():
    il, *_rest = build_driver_shape()
    wide_row = Block(200)
    wide_row.add(set_var("wide_state", var("state")))
    condition = Expr(
        "MLIL_CMP_E",
        left=var("wide_state"),
        right=const(0x1111, size=8),
    )
    wide_row.add(Expr("MLIL_IF", [condition], condition=condition, true=40, false=20))
    il.basic_blocks.append(wide_row)
    for instruction in wide_row:
        il.instructions.append(instruction)
        il.by_index[instruction.instr_index] = instruction
        if instruction.operation == MediumLevelILOperation.MLIL_SET_VAR:
            il.defs.setdefault(instruction.dest, []).append(instruction)

    analysis = driver._analyze_driver_dispatcher(il)

    assert analysis is not None
    assert wide_row.start not in analysis["dispatcher_starts"]


def test_driver_deflatten_hook_routes_stack_tokens_through_range_dispatcher():
    il, entry_jump, branch, real_b_jump, real_b, real_c = build_driver_shape(
        range_dispatch=True
    )
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    entry_plan = next(plan for plan in plans if plan.get("entry"))
    assert entry_plan["exit_jumps"] == (entry_jump,)
    assert entry_plan["target_bb"].start == 40
    conditional = next(plan for plan in plans if plan["kind"] == "if_else")
    assert conditional["if_il"] is branch
    assert conditional["true_target"].start == real_b.start
    assert conditional["false_target"].start == real_c.start
    uncond = next(plan for plan in plans if real_b_jump in plan.get("exit_jumps", ()))
    assert uncond["target_bb"].start == real_c.start


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


def test_driver_deflatten_hook_rejects_conditional_tail_writing_other_state():
    il, _entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    true_tail = next(bb for bb in il.basic_blocks if bb.start == 50)
    tail_goto = true_tail.instructions[-1]
    other_write = set_var("other_state", const(0x4444))
    other_write.instr_index = tail_goto.instr_index
    other_write.il_basic_block = true_tail
    tail_goto.instr_index += 1
    true_tail.instructions.insert(-1, other_write)
    true_tail.end += 1
    il.instructions.append(other_write)
    il.by_index[other_write.instr_index] = other_write
    il.by_index[tail_goto.instr_index] = tail_goto
    il.defs["other_state"] = [other_write]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_conditional_rejects_arm_with_external_entry():
    il, _entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    true_tail = next(bb for bb in il.basic_blocks if bb.start == 50)
    external = Block(200)
    external_jump = external.add(goto())
    link(external, true_tail)
    il.basic_blocks.append(external)
    il.instructions.append(external_jump)
    il.by_index[external_jump.instr_index] = external_jump
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_conditional_rejects_state_observed_outside_dispatcher():
    il, _entry_jump, branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    observed = set_var("observed", var("state"))
    observed.instr_index = tail.instr_index
    observed.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, observed)
    real_b.end += 1
    il.instructions.append(observed)
    il.by_index[observed.instr_index] = observed
    il.by_index[tail.instr_index] = tail
    il.defs["observed"] = [observed]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_transition_rejects_path_without_state_token_definition():
    il, _entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    false_tail = next(bb for bb in il.basic_blocks if bb.start == 60)
    false_definition = false_tail.instructions[0]
    false_definition.operation = MediumLevelILOperation.MLIL_NOP
    il.defs["next"].remove(false_definition)
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not branch.il_basic_block for plan in plans)


def test_driver_deflatten_hook_rejects_conditional_arm_with_looping_exit_path():
    il, _entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    store_tail = next(bb for bb in il.basic_blocks if bb.start == 70)
    loop = next(bb for bb in il.basic_blocks if bb.start == 100)
    old_jump = store_tail.instructions[-1]
    loop_or_dispatch = if_cond(loop.start, store_tail.start)
    loop_or_dispatch.instr_index = old_jump.instr_index
    loop_or_dispatch.il_basic_block = store_tail
    store_tail.instructions[-1] = loop_or_dispatch
    il.instructions[il.instructions.index(old_jump)] = loop_or_dispatch
    il.by_index[loop_or_dispatch.instr_index] = loop_or_dispatch
    old_edge = store_tail.outgoing_edges.pop()
    old_edge.target.incoming_edges.remove(old_edge)
    link(store_tail, loop)
    link(store_tail, store_tail)
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_state_pointer_redefinition_invalidates_state_store():
    il, entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    entry = next(bb for bb in il.basic_blocks if bb.start == 0)
    other_pointer = set_var("state_ptr", const(0xDEADBEEF))
    other_pointer.instr_index = entry_jump.instr_index
    other_pointer.il_basic_block = entry
    entry_jump.instr_index += 1
    entry.instructions.insert(-1, other_pointer)
    entry.end += 1
    il.instructions.append(other_pointer)
    il.by_index[other_pointer.instr_index] = other_pointer
    il.by_index[entry_jump.instr_index] = entry_jump
    il.defs["state_ptr"].append(other_pointer)
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_transition_rejects_ambiguous_state_pointer_store():
    il, entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    entry = next(bb for bb in il.basic_blocks if bb.start == 0)
    other_pointer = set_var("state_ptr", const(0xDEADBEEF))
    other_pointer.instr_index = entry_jump.instr_index
    other_pointer.il_basic_block = entry
    entry_jump.instr_index += 1
    entry.instructions.insert(-1, other_pointer)
    entry.end += 1
    il.instructions.append(other_pointer)
    il.by_index[other_pointer.instr_index] = other_pointer
    il.by_index[entry_jump.instr_index] = entry_jump
    il.defs["state_ptr"].append(other_pointer)

    state_write = set_var("state", const(0x3333))
    state_write.instr_index = real_b.start
    state_write.il_basic_block = real_b
    for ins in real_b.instructions:
        ins.instr_index += 1
        il.by_index[ins.instr_index] = ins
    real_b.instructions.insert(0, state_write)
    real_b.end += 1
    il.instructions.append(state_write)
    il.by_index[state_write.instr_index] = state_write
    il.defs["state"].append(state_write)
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


def test_driver_state_pointer_definition_must_dominate_store():
    il, _entry_jump, branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    entry = next(bb for bb in il.basic_blocks if bb.start == 0)
    old_definition = entry.instructions[1]
    old_definition.operation = MediumLevelILOperation.MLIL_NOP
    il.defs["state_ptr"].remove(old_definition)
    real_c = next(bb for bb in il.basic_blocks if bb.start == 90)
    tail = real_c.instructions[-1]
    late_definition = set_var("state_ptr", addr_of("state"))
    late_definition.instr_index = tail.instr_index
    late_definition.il_basic_block = real_c
    tail.instr_index += 1
    real_c.instructions.insert(-1, late_definition)
    real_c.end += 1
    il.instructions.append(late_definition)
    il.by_index[late_definition.instr_index] = late_definition
    il.by_index[tail.instr_index] = tail
    il.defs["state_ptr"] = [late_definition]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_dispatcher_alias_observer_rejects_redirection():
    il, _entry_jump, branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    observed = set_var("observed", var("temp0"))
    observed.instr_index = tail.instr_index
    observed.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, observed)
    real_b.end += 1
    il.instructions.append(observed)
    il.by_index[observed.instr_index] = observed
    il.by_index[tail.instr_index] = tail
    il.defs["observed"] = [observed]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_dispatcher_alias_definition_must_cover_every_entry():
    il, entry_jump, _branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    entry = next(bb for bb in il.basic_blocks if bb.start == 0)
    row_a = next(bb for bb in il.basic_blocks if bb.start == 10)
    alias_definition = row_a.instructions.pop(1)
    row_if = row_a.instructions[-1]
    row_if.instr_index -= 1
    row_a.end -= 1
    il.by_index[row_if.instr_index] = row_if

    alias_definition.instr_index = entry_jump.instr_index
    alias_definition.il_basic_block = entry
    entry_jump.instr_index += 1
    entry.instructions.insert(-1, alias_definition)
    entry.end += 1
    il.by_index[alias_definition.instr_index] = alias_definition
    il.by_index[entry_jump.instr_index] = entry_jump
    func = types.SimpleNamespace(start=0x36D10)

    assert driver.plan_deflatten_redirections(None, func, il) == []


def test_driver_transition_rejects_struct_store_to_state():
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    struct_store = Expr(
        "MLIL_STORE_STRUCT",
        [var("ptr_b"), const(0x1111)],
        dest=var("ptr_b"),
        offset=0,
        src=const(0x1111),
    )
    struct_store.instr_index = tail.instr_index
    struct_store.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, struct_store)
    real_b.end += 1
    il.instructions.append(struct_store)
    il.by_index[struct_store.instr_index] = struct_store
    il.by_index[tail.instr_index] = tail
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


def test_driver_transition_rejects_arithmetic_state_pointer_store():
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    pointer_copy, state_store, tail = real_b.instructions
    state_write = set_var("state", const(0x2222))
    arithmetic_pointer = set_var(
        "q",
        binary("MLIL_ADD", var("ptr_b"), const(0)),
    )
    state_store.dest = var("q")
    state_store.children = [state_store.dest, state_store.src]
    real_b.instructions = [
        state_write,
        pointer_copy,
        arithmetic_pointer,
        state_store,
        tail,
    ]
    real_b.end = real_b.start + len(real_b.instructions)
    for index, instruction in enumerate(real_b.instructions, real_b.start):
        instruction.instr_index = index
        instruction.il_basic_block = real_b
        il.by_index[index] = instruction
    il.instructions.extend((state_write, arithmetic_pointer))
    il.defs.setdefault("state", []).append(state_write)
    il.defs["q"] = [arithmetic_pointer]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


def test_driver_transition_rejects_field_value_as_exact_state_pointer():
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    pointer_copy = real_b.instructions[0]
    field_pointer = Expr(
        "MLIL_VAR_FIELD",
        src="state_ptr",
        offset=4,
        size=8,
    )
    pointer_copy.src = field_pointer
    pointer_copy.children = [field_pointer]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


def test_driver_transition_rejects_truncated_state_pointer_copy():
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    pointer_copy = real_b.instructions[0]
    pointer_copy.size = 4
    pointer_copy.src.size = 8
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


def test_driver_transition_accepts_exact_zero_offset_state_pointer_store():
    il, _entry_jump, _branch, real_b_jump, real_b, real_c = build_driver_shape()
    pointer_copy, state_store, tail = real_b.instructions
    arithmetic_pointer = set_var(
        "q",
        binary("MLIL_ADD", var("ptr_b"), const(0)),
    )
    state_store.dest = var("q")
    state_store.children = [state_store.dest, state_store.src]
    real_b.instructions = [pointer_copy, arithmetic_pointer, state_store, tail]
    real_b.end = real_b.start + len(real_b.instructions)
    for index, instruction in enumerate(real_b.instructions, real_b.start):
        instruction.instr_index = index
        instruction.il_basic_block = real_b
        il.by_index[index] = instruction
    il.instructions.append(arithmetic_pointer)
    il.defs["q"] = [arithmetic_pointer]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    plan = next(plan for plan in plans if real_b_jump in plan.get("exit_jumps", ()))
    assert plan["target_bb"] is real_c
    assert plan["state_token"] == (0x3333, 4)


def test_driver_conditional_rejects_partial_state_write_in_successor():
    il, _entry_jump, branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    partial_write = Expr(
        "MLIL_SET_VAR_FIELD",
        [const(0x1111)],
        dest="state",
        offset=0,
        src=const(0x1111),
    )
    partial_write.instr_index = tail.instr_index
    partial_write.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, partial_write)
    real_b.end += 1
    il.instructions.append(partial_write)
    il.by_index[partial_write.instr_index] = partial_write
    il.by_index[tail.instr_index] = tail
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_transition_rejects_state_address_passed_to_call():
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    state_call = call(const(0x5000), [addr_of_field("state", 0)])
    state_call.instr_index = tail.instr_index
    state_call.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, state_call)
    real_b.end += 1
    il.instructions.append(state_call)
    il.by_index[state_call.instr_index] = state_call
    il.by_index[tail.instr_index] = tail
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


@pytest.mark.parametrize("operation", ["MLIL_UNIMPL", "MLIL_UNIMPL_MEM"])
@pytest.mark.parametrize("nested", [False, True])
def test_driver_transition_rejects_unmodeled_instruction(operation, nested):
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    unmodeled_expr = Expr(operation)
    unmodeled = (
        set_var("unmodeled_result", unmodeled_expr)
        if nested
        else unmodeled_expr
    )
    unmodeled.instr_index = tail.instr_index
    unmodeled.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, unmodeled)
    real_b.end += 1
    il.instructions.append(unmodeled)
    il.by_index[unmodeled.instr_index] = unmodeled
    il.by_index[tail.instr_index] = tail
    if nested:
        il.defs["unmodeled_result"] = [unmodeled]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


@pytest.mark.parametrize(
    "mutate_op",
    ["MLIL_CALL", "MLIL_UNIMPL_MEM", "MLIL_TRAP", "MLIL_BP"],
)
def test_driver_transition_rejects_unknown_effect_after_retained_state_address(
    mutate_op,
):
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    entry = il.basic_blocks[0]
    entry_tail = entry.instructions[-1]
    register = call(const(0x5000), [addr_of_field("state", 0)])
    register.instr_index = entry_tail.instr_index
    register.il_basic_block = entry
    entry_tail.instr_index += 1
    entry.instructions.insert(-1, register)
    entry.end += 1

    mutate_tail = real_b.instructions[-1]
    mutate = (
        call(const(0x6000), [])
        if mutate_op == "MLIL_CALL"
        else Expr(mutate_op)
    )
    mutate.instr_index = mutate_tail.instr_index
    mutate.il_basic_block = real_b
    mutate_tail.instr_index += 1
    real_b.instructions.insert(-1, mutate)
    real_b.end += 1

    il.instructions.extend((register, mutate))
    for instruction in (register, entry_tail, mutate, mutate_tail):
        il.by_index[instruction.instr_index] = instruction
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


@pytest.mark.parametrize("hidden_source_kind", ["token", "address"])
def test_driver_transition_rejects_store_through_reloaded_state_address(
    hidden_source_kind,
):
    il, _entry_jump, _branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    publish = store(const(0x9000), addr_of_field("state", 0))
    pointer_definition = set_var(
        "escaped_pointer",
        load(const(0x9000), size=8),
    )
    hidden_state_write = store(
        var("escaped_pointer"),
        (
            const(0x1111)
            if hidden_source_kind == "token"
            else addr_of_field("state", 0)
        ),
    )
    for offset, instruction in enumerate(
        (publish, pointer_definition, hidden_state_write),
    ):
        instruction.instr_index = tail.instr_index + offset
        instruction.il_basic_block = real_b
        il.by_index[instruction.instr_index] = instruction
    tail.instr_index += 3
    il.by_index[tail.instr_index] = tail
    real_b.instructions[-1:-1] = [publish, pointer_definition, hidden_state_write]
    real_b.end += 3
    il.instructions.extend((publish, pointer_definition, hidden_state_write))
    il.defs["escaped_pointer"] = [pointer_definition]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("obb") is not real_b for plan in plans)


def test_driver_conditional_rejects_state_field_address_escape():
    il, _entry_jump, branch, _real_b_jump, real_b, _real_c = build_driver_shape()
    tail = real_b.instructions[-1]
    observed = set_var("observed", addr_of_field("state", 4))
    observed.instr_index = tail.instr_index
    observed.il_basic_block = real_b
    tail.instr_index += 1
    real_b.instructions.insert(-1, observed)
    real_b.end += 1
    il.instructions.append(observed)
    il.by_index[observed.instr_index] = observed
    il.by_index[tail.instr_index] = tail
    il.defs["observed"] = [observed]
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(plan.get("if_il") is not branch for plan in plans)


def test_driver_dispatcher_row_rejects_unrelated_assignment():
    il, _entry_jump, _branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    row_a = next(bb for bb in il.basic_blocks if bb.start == 10)
    tail = row_a.instructions[-1]
    important = set_var("important", const(1))
    important.instr_index = tail.instr_index
    important.il_basic_block = row_a
    tail.instr_index += 1
    row_a.instructions.insert(-1, important)
    row_a.end += 1
    il.instructions.append(important)
    il.by_index[important.instr_index] = important
    il.by_index[tail.instr_index] = tail
    il.defs["important"] = [important]
    func = types.SimpleNamespace(start=0x36D10)

    assert driver.plan_deflatten_redirections(None, func, il) == []


def test_driver_analysis_does_not_hide_impure_non_dominant_comparison_row():
    il, _entry_jump, _branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    extra_row = Block(110)
    alias = extra_row.add(set_var("extra_state", var("state")))
    extra_row.add(call(const(0x5000), []))
    left = var("extra_state")
    right = const(0x1111000011110001, size=8)
    condition = Expr(
        "MLIL_CMP_E",
        [left, right],
        left=left,
        right=right,
    )
    extra_row.add(
        Expr(
            "MLIL_IF",
            [condition],
            condition=condition,
            true=80,
            false=100,
        )
    )
    il.basic_blocks.append(extra_row)
    il.instructions.extend(extra_row.instructions)
    for instruction in extra_row:
        il.by_index[instruction.instr_index] = instruction
    il.defs["extra_state"] = [alias]

    analysis = driver._analyze_driver_dispatcher(il)

    assert analysis is not None
    assert extra_row.start not in analysis["dispatcher_starts"]


def test_driver_entry_rejects_external_entry_into_intermediate_region():
    il, entry_jump, _branch, _real_b_jump, _real_b, _real_c = build_driver_shape()
    entry = next(bb for bb in il.basic_blocks if bb.start == 0)
    dispatcher = next(bb for bb in il.basic_blocks if bb.start == 10)
    old_edge = entry.outgoing_edges.pop()
    old_edge.target.incoming_edges.remove(old_edge)
    middle = Block(200)
    middle.add(set_var("middle_value", const(1)))
    middle.add(goto())
    exit_tail = Block(210)
    exit_tail.add(set_var("exit_value", const(2)))
    exit_tail.add(goto())
    external = Block(220)
    external.add(goto())
    link(entry, middle)
    link(middle, exit_tail)
    link(exit_tail, dispatcher)
    link(external, middle)
    for block in (middle, exit_tail, external):
        il.basic_blocks.append(block)
        for instruction in block:
            il.instructions.append(instruction)
            il.by_index[instruction.instr_index] = instruction
            if instruction.operation.name == "MLIL_SET_VAR":
                il.defs.setdefault(instruction.dest, []).append(instruction)
    func = types.SimpleNamespace(start=0x36D10)

    plans = driver.plan_deflatten_redirections(None, func, il)

    assert all(not plan.get("entry") for plan in plans)
    assert entry_jump not in {
        jump
        for plan in plans
        for jump in plan.get("exit_jumps", ())
    }


def test_driver_global_constant_hook_plans_driver_blob_base_slot():
    bv = FakeBv()
    base_slot = 0x2983C8
    key_slot = 0x2983F8
    values = {
        base_slot: 0x206CA19FC1FAF2B9,
        key_slot: 0xAFE4239D807FA475,
    }
    bv.data_vars[base_slot] = DataVar("void*")
    bv.data_vars[key_slot] = DataVar("int64_t")
    bv.sections[base_slot] = [Section(".data")]
    bv.sections[key_slot] = [Section(".data")]
    bv.memory[base_slot] = values[base_slot].to_bytes(8, "little")
    bv.memory[key_slot] = values[key_slot].to_bytes(8, "little")
    base_load = set_var("x9", load(const(base_slot), address=0x2F670), address=0x2F670)
    key_load = set_var("x12", load(const(key_slot), address=0x2F674), address=0x2F674)
    source_arg = set_var("x1", binary("MLIL_ADD", var("x9"), var("x12")), address=0x2F6A4)
    il = one_block(
        base_load,
        key_load,
        source_arg,
        call(const(0x579CC), [const(0x2C8810), var("x1")], address=0x2F6A8),
    )

    assert driver.plan_global_constant_slots(bv, il) == [
        {
            "slot_addr": base_slot,
            "type": "void const* const",
        },
        {
            "slot_addr": key_slot,
            "type": "int64_t const",
        },
    ]


def test_driver_global_constant_hook_rejects_a_slot_written_in_current_mlil():
    bv = FakeBv()
    slot = 0x2983C8
    bv.data_vars[slot] = DataVar("void*")
    bv.sections[slot] = [Section(".data")]
    bv.memory[slot] = (0x1234).to_bytes(8, "little")
    slot_load = set_var("x9", load(const(slot)))
    source_arg = set_var("x1", binary("MLIL_ADD", var("x9"), const(8)))
    il = one_block(
        slot_load,
        source_arg,
        store(const(slot), const(0x5678)),
        call(const(0x579CC), [const(0x2C8810), var("x1")]),
    )

    assert driver.plan_global_constant_slots(bv, il) == []


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
