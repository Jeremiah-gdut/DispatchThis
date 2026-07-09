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


def const(value, size=4):
    return Expr("MLIL_CONST", constant=value, size=size)


def var(name):
    return Expr("MLIL_VAR", src=name)


def addr_of(name):
    return Expr("MLIL_ADDRESS_OF", src=name)


def set_var(dest, src):
    return Expr("MLIL_SET_VAR", [src], dest=dest, src=src, vars_written={dest})


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


if __name__ == "__main__":
    test_driver_deflatten_hook_handles_stack_state_stores()
    test_driver_deflatten_hook_skips_conditional_tail_with_real_store()
