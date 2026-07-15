import types
from copy import copy

import pytest
from binaryninja import MediumLevelILOperation

from conftest import load_plugin_module


deflatten = load_plugin_module("plugins.DispatchThis.passes.medium.deflatten")

compute_redirections = deflatten.compute_redirections
rewrite_redirections_mlil = deflatten.rewrite_redirections_mlil


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    _next_index = 1

    def __init__(self, op, **attrs):
        self.operation = MediumLevelILOperation.__members__.get(op, Op(op))
        self.expr_index = attrs.pop("expr_index", Expr._next_index)
        Expr._next_index += 1
        self.address = attrs.pop("address", 0x1000 + self.expr_index)
        self.size = attrs.pop("size", 8)
        self.vars_written = []
        self.__dict__.update(attrs)

    def traverse(self, visit):
        yield visit(self)
        for value in self.__dict__.values():
            if isinstance(value, Expr):
                yield from value.traverse(visit)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    if isinstance(child, Expr):
                        yield from child.traverse(visit)


class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target


class Block:
    def __init__(self, start, *instructions):
        self.start = start
        self.instructions = list(instructions)
        self.end = start + len(self.instructions)
        self.incoming_edges = []
        self.outgoing_edges = []
        self.il_function = None
        for instr in self.instructions:
            instr.il_basic_block = self

    def __iter__(self):
        return iter(self.instructions)

    def __getitem__(self, index):
        return self.instructions[index - self.start]


class FakeMlil:
    def __init__(self, blocks, defs):
        self.basic_blocks = list(blocks)
        self._by_index = {}
        self._defs = defs
        self.source_function = types.SimpleNamespace(mlil=self)
        self.replacements = []
        for block in self.basic_blocks:
            block.il_function = self
            for instr in block:
                instr.function = self
                self._by_index[instr.instr_index] = instr
        # The compact dispatcher fixtures keep copy definitions out of the
        # instruction lists. Model them as row-local statements so reaching-
        # definition checks see the same shape as real MLIL.
        for block in self.basic_blocks:
            if not block.instructions:
                continue
            tail = block.instructions[-1]
            if tail.operation.name != "MLIL_IF":
                continue
            condition = getattr(tail, "condition", None)
            for operand in (
                getattr(condition, "left", None),
                getattr(condition, "right", None),
            ):
                if getattr(getattr(operand, "operation", None), "name", None) != "MLIL_VAR":
                    continue
                for offset, definition in enumerate(defs.get(operand.src, ())):
                    if hasattr(definition, "il_basic_block"):
                        continue
                    definition.il_basic_block = block
                    definition.instr_index = tail.instr_index - offset - 1
                    definition.function = self

    def __iter__(self):
        return iter(self.basic_blocks)

    def __getitem__(self, index):
        return self._by_index[index]

    def get_basic_block_at(self, index):
        instruction = self._by_index.get(index)
        return getattr(instruction, "il_basic_block", None)

    def get_var_definitions(self, var):
        return self._defs.get(var, [])

    def replace_expr(self, expr_index, expr):
        self.replacements.append((expr_index, expr))

    def goto(self, label, loc):
        return ("goto", label.operand, loc)

    def if_expr(self, cond, true_label, false_label, loc):
        return ("if", cond, true_label.operand, false_label.operand, loc)

    def copy_expr(self, expr):
        return ("copy", expr)

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        self.ssa_generated = True


class CopiedMlil:
    def __init__(self):
        self.outputs = {}

    def get_label_for_source_instruction(self, instr_index):
        return types.SimpleNamespace(operand=("copied-label", instr_index))

    def goto(self, label, loc):
        return ("goto", label.operand, loc)

    def if_expr(self, cond, true_label, false_label, loc):
        return ("if", cond, true_label.operand, false_label.operand, loc)

    def var(self, size, variable, loc):
        return ("var", size, variable, loc)

    def const(self, size, value, loc):
        return ("const", size, value, loc)

    def compare_equal(self, size, left, right, loc):
        return ("cmp_e", size, left, right, loc)

    def copy_expr(self, expr):
        return ("copy", expr)

    def nop(self, loc):
        return ("nop", loc)


class FakeFunc:
    def __init__(self, mlil):
        self.medium_level_il = mlil
        self.mlil = mlil
        self.start = 0x4000


class FakeBv:
    pass


def var(name):
    return Expr("MLIL_VAR", src=name)


def address_of_field(name, offset=0):
    return Expr("MLIL_ADDRESS_OF_FIELD", src=name, offset=offset)


def const(value, size=8):
    return Expr("MLIL_CONST", constant=value, size=size)


def cmp_e(name, value, size=8):
    return Expr("MLIL_CMP_E", left=var(name), right=const(value, size), size=1)


def cmp_ne(name, value, size=8):
    return Expr("MLIL_CMP_NE", left=var(name), right=const(value, size), size=1)


def cmp_range(op, name, value, size=8, var_on_left=True):
    variable = var(name)
    bound = const(value, size)
    left, right = (variable, bound) if var_on_left else (bound, variable)
    return Expr(op, left=left, right=right, size=1)


def set_var(name, src, index):
    ins = Expr("MLIL_SET_VAR", dest=name, src=src, instr_index=index)
    ins.vars_written = [name]
    return ins


def goto(index):
    return Expr("MLIL_GOTO", instr_index=index, dest=index)


def if_instr(cond, true_index, false_index, index):
    return Expr("MLIL_IF", condition=cond, true=true_index, false=false_index, instr_index=index)


def link(source, *targets):
    for target in targets:
        edge = Edge(source, target)
        source.outgoing_edges.append(edge)
        target.incoming_edges.append(edge)


def append_block(mlil, block):
    block.il_function = mlil
    for instruction in block:
        instruction.function = mlil
        mlil._by_index[instruction.instr_index] = instruction
    mlil.basic_blocks.append(block)


def build_uncond_function():
    d1 = Block(0, if_instr(cmp_e("t1", 0x1111000011110001), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", 0x2222000022220002), 20, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", 0x3333000033330003), 30, 99, 2))
    obb1 = Block(10, set_var("state", const(0x2222000022220002), 10), goto(11))
    obb2 = Block(20, set_var("state", const(0x3333000033330003), 20), goto(21))
    obb3 = Block(30, set_var("state", const(0x1111000011110001), 30), goto(31))
    exit_bb = Block(99, goto(99))
    link(d1, obb1, d2)
    link(d2, obb2, d3)
    link(d3, obb3, exit_bb)
    link(obb1, d1)
    link(obb2, d1)
    link(obb3, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "state": [obb1[10], obb2[20], obb3[30]],
    }
    mlil = FakeMlil([d1, d2, d3, obb1, obb2, obb3, exit_bb], defs)
    return FakeFunc(mlil), obb1, obb2


def build_shared_state_latch_function(mode="pure"):
    func, obb1, obb2 = build_uncond_function()
    obbs = [bb for bb in func.mlil.basic_blocks if bb.start in {10, 20, 30}]
    dispatcher = func.mlil.get_basic_block_at(0)
    for obb in obbs:
        write = obb[obb.start]
        write.dest = "next_state"
        write.vars_written = ["next_state"]
        for edge in tuple(obb.outgoing_edges):
            edge.target.incoming_edges.remove(edge)
        obb.outgoing_edges.clear()

    tail_instructions = [goto(50)]
    if mode == "side_effect":
        tail_instructions.insert(
            0,
            Expr("MLIL_CALL", dest=const(0x5000), params=[], instr_index=50),
        )
        tail_instructions[-1].instr_index = 51
    shared_tail = Block(50, *tail_instructions)
    copy_source = var("next_state")
    if mode == "field_copy":
        copy_source = Expr(
            "MLIL_VAR_FIELD",
            src="next_state",
            offset=0,
            size=4,
        )
    if mode == "commit_call":
        commit = Block(
            59,
            Expr("MLIL_CALL", dest=const(0x5000), params=[], instr_index=59),
            set_var("state", copy_source, 60),
            goto(61),
        )
    else:
        commit = Block(60, set_var("state", copy_source, 60), goto(61))
    for obb in obbs:
        link(obb, shared_tail)
    link(shared_tail, commit)
    link(commit, dispatcher)

    for block in (shared_tail, commit):
        block.il_function = func.mlil
        for instruction in block:
            instruction.function = func.mlil
            func.mlil._by_index[instruction.instr_index] = instruction
    func.mlil.basic_blocks.extend((shared_tail, commit))
    func.mlil._defs["next_state"] = [obb[obb.start] for obb in obbs]
    func.mlil._defs["state"] = [commit[60]]
    return func, obb1, obb2, shared_tail, commit


def build_ne_leaf_function():
    d1 = Block(0, if_instr(cmp_e("t1", 0x1111000011110001), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_ne("t2", 0x2222000022220002), 2, 20, 1))
    d3 = Block(2, if_instr(cmp_e("t3", 0x3333000033330003), 30, 99, 2))
    obb1 = Block(10, set_var("state", const(0x2222000022220002), 10), goto(11))
    obb2 = Block(20, set_var("state", const(0x3333000033330003), 20), goto(21))
    obb3 = Block(30, set_var("state", const(0x1111000011110001), 30), goto(31))
    exit_bb = Block(99, goto(99))
    link(d1, obb1, d2)
    link(d2, d3, obb2)
    link(d3, obb3, exit_bb)
    link(obb1, d1)
    link(obb2, d1)
    link(obb3, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "state": [obb1[10], obb2[20], obb3[30]],
    }
    mlil = FakeMlil([d1, d2, d3, obb1, obb2, obb3, exit_bb], defs)
    return FakeFunc(mlil), obb1, obb2


def build_range_dispatch_function():
    d1 = Block(0, if_instr(cmp_range("MLIL_CMP_ULT", "t1", 0x2000), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_range("MLIL_CMP_UGT", "t2", 0x3000, var_on_left=False), 20, 2, 1))
    d3 = Block(2, if_instr(cmp_range("MLIL_CMP_ULE", "t3", 0x3FFF), 30, 40, 2))
    obb1 = Block(10, set_var("state", const(0x2800), 10), goto(11))
    obb2 = Block(20, set_var("state", const(0x3800), 20), goto(21))
    obb3 = Block(30, set_var("state", const(0x4800), 30), goto(31))
    obb4 = Block(40, set_var("state", const(0x1800), 40), goto(41))
    link(d1, obb1, d2)
    link(d2, obb2, d3)
    link(d3, obb3, obb4)
    link(obb1, d1)
    link(obb2, d1)
    link(obb3, d1)
    link(obb4, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "state": [obb1[10], obb2[20], obb3[30], obb4[40]],
    }
    mlil = FakeMlil([d1, d2, d3, obb1, obb2, obb3, obb4], defs)
    return FakeFunc(mlil), obb1, obb2, obb3, obb4


def build_comparison_dispatch_function(op, var_on_left, bound, true_token, false_token):
    d1 = Block(
        0,
        if_instr(
            cmp_range(op, "t1", bound, size=1, var_on_left=var_on_left),
            10,
            1,
            0,
        ),
    )
    d2 = Block(1, if_instr(cmp_e("t2", false_token, size=1), 20, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", true_token, size=1), 30, 40, 2))
    true_leaf = Block(10, set_var("state", const(true_token, 1), 10), goto(11))
    false_leaf = Block(20, set_var("state", const(false_token, 1), 20), goto(21))
    fallback = Block(30, set_var("state", const(true_token, 1), 30), goto(31))
    exit_bb = Block(40, goto(40))
    link(d1, true_leaf, d2)
    link(d2, false_leaf, d3)
    link(d3, fallback, exit_bb)
    link(true_leaf, d1)
    link(false_leaf, d1)
    link(fallback, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "state": [true_leaf[10], false_leaf[20], fallback[30]],
    }
    mlil = FakeMlil([d1, d2, d3, true_leaf, false_leaf, fallback, exit_bb], defs)
    return FakeFunc(mlil), true_leaf, false_leaf


def build_multi_exit_function():
    token_a = 0x1111000011110001
    token_b = 0x2222000022220002
    token_c = 0x3333000033330003
    d1 = Block(0, if_instr(cmp_e("t1", token_a), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", token_b), 30, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", token_c), 40, 99, 2))
    head = Block(10, if_instr(var("program_cond"), 11, 20, 10))
    true_tail = Block(11, set_var("state", const(token_b), 11), goto(12))
    false_tail = Block(20, set_var("state", const(token_b), 20), goto(21))
    obb2 = Block(30, set_var("state", const(token_c), 30), goto(31))
    obb3 = Block(40, set_var("state", const(token_a), 40), goto(41))
    exit_bb = Block(99, goto(99))
    link(d1, head, d2)
    link(d2, obb2, d3)
    link(d3, obb3, exit_bb)
    link(head, true_tail, false_tail)
    link(true_tail, d1)
    link(false_tail, d1)
    link(obb2, d1)
    link(obb3, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "state": [true_tail[11], false_tail[20], obb2[30], obb3[40]],
    }
    mlil = FakeMlil(
        [d1, d2, d3, head, true_tail, false_tail, obb2, obb3, exit_bb],
        defs,
    )
    return FakeFunc(mlil), head, true_tail[12], false_tail[21], obb2


def build_multi_exit_entry_function():
    func, target, _true_jump, _false_jump, _next_target = build_multi_exit_function()
    dispatcher_entry = func.mlil.basic_blocks[0]
    entry = Block(200, if_instr(var("entry_cond"), 210, 220, 200))
    true_tail = Block(
        210,
        set_var("state", const(0x1111000011110001), 210),
        goto(211),
    )
    false_tail = Block(
        220,
        set_var("state", const(0x1111000011110001), 220),
        goto(221),
    )
    link(entry, true_tail, false_tail)
    link(true_tail, dispatcher_entry)
    link(false_tail, dispatcher_entry)
    func.mlil.basic_blocks[:0] = [entry, true_tail, false_tail]
    for block in (entry, true_tail, false_tail):
        block.il_function = func.mlil
        for ins in block:
            ins.function = func.mlil
            func.mlil._by_index[ins.instr_index] = ins
    func.mlil._defs["state"].extend((true_tail[210], false_tail[220]))
    return func, entry, true_tail[211], false_tail[221], target


def build_entry_function():
    entry = Block(90, set_var("state", const(0x1111000011110001), 90), goto(91))
    d1 = Block(0, if_instr(cmp_e("t1", 0x1111000011110001), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", 0x2222000022220002), 20, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", 0x3333000033330003), 30, 99, 2))
    obb1 = Block(10, set_var("state", const(0x2222000022220002), 10), goto(11))
    obb2 = Block(20, set_var("state", const(0x3333000033330003), 20), goto(21))
    obb3 = Block(30, set_var("state", const(0x1111000011110001), 30), goto(31))
    exit_bb = Block(99, goto(99))
    link(entry, d1)
    link(d1, obb1, d2)
    link(d2, obb2, d3)
    link(d3, obb3, exit_bb)
    link(obb1, d1)
    link(obb2, d1)
    link(obb3, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "state": [entry[90], obb1[10], obb2[20], obb3[30]],
    }
    mlil = FakeMlil([entry, d1, d2, d3, obb1, obb2, obb3, exit_bb], defs)
    return FakeFunc(mlil), entry, obb1


def build_cond_function():
    d1 = Block(0, if_instr(cmp_e("t1", 0x1111000011110001), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", 0x2222000022220002), 20, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", 0x3333000033330003), 30, 99, 2))
    chooser = Block(10, if_instr(var("program_cond"), 11, 12, 10))
    true_arm = Block(11, set_var("next_state", const(0x2222000022220002), 11), goto(12))
    false_arm = Block(12, set_var("next_state", const(0x3333000033330003), 12), goto(13))
    join = Block(13, set_var("state", var("next_state"), 13), goto(14))
    obb2 = Block(20, goto(20))
    obb3 = Block(30, goto(30))
    exit_bb = Block(99, goto(99))
    link(d1, chooser, d2)
    link(d2, obb2, d3)
    link(d3, obb3, exit_bb)
    link(chooser, true_arm, false_arm)
    link(true_arm, join)
    link(false_arm, join)
    link(join, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "next_state": [true_arm[11], false_arm[12]],
        "state": [join[13]],
    }
    mlil = FakeMlil([d1, d2, d3, chooser, true_arm, false_arm, join, obb2, obb3, exit_bb], defs)
    return FakeFunc(mlil), chooser, obb2, obb3


def build_cond_function_with_shared_semantic_tail():
    func, chooser, obb2, obb3 = build_cond_function()
    join = next(bb for bb in func.mlil.basic_blocks if bb.start == 13)
    jump = join.instructions[-1]
    del func.mlil._by_index[jump.instr_index]
    jump.instr_index += 1
    semantic_write = set_var("loop_index", const(1), 14)
    semantic_write.il_basic_block = join
    semantic_write.function = func.mlil
    join.instructions.insert(-1, semantic_write)
    join.end += 1
    func.mlil._by_index[semantic_write.instr_index] = semantic_write
    func.mlil._by_index[jump.instr_index] = jump
    func.mlil._defs["loop_index"] = [semantic_write]
    return func, chooser, join, obb2, obb3


def build_cond_function_with_arm_selected_shared_semantics():
    d1 = Block(0, if_instr(cmp_e("t1", 0x1111000011110001), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", 0x2222000022220002), 40, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", 0x3333000033330003), 50, 99, 2))
    chooser = Block(10, if_instr(var("program_cond"), 11, 20, 10))
    true_arm = Block(
        11,
        set_var("next_state", const(0x2222000022220002), 11),
        set_var("next_value", const(1), 12),
        set_var("preserved_scratch", const(11), 13),
        goto(14),
    )
    false_arm = Block(
        20,
        set_var("next_state", const(0x3333000033330003), 20),
        set_var("next_value", const(2), 21),
        set_var("preserved_scratch", const(22), 22),
        goto(23),
    )
    join = Block(
        30,
        set_var("state", var("next_state"), 30),
        set_var("semantic_value", var("next_value"), 31),
        goto(32),
    )
    obb2 = Block(40, goto(40))
    obb3 = Block(50, goto(50))
    exit_bb = Block(99, goto(99))
    link(d1, chooser, d2)
    link(d2, obb2, d3)
    link(d3, obb3, exit_bb)
    link(chooser, true_arm, false_arm)
    link(true_arm, join)
    link(false_arm, join)
    link(join, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "next_state": [true_arm[11], false_arm[20]],
        "next_value": [true_arm[12], false_arm[21]],
        "preserved_scratch": [true_arm[13], false_arm[22]],
        "state": [join[30]],
        "semantic_value": [join[31]],
    }
    mlil = FakeMlil(
        [d1, d2, d3, chooser, true_arm, false_arm, join, obb2, obb3, exit_bb],
        defs,
    )
    return FakeFunc(mlil), chooser, join, obb2, obb3


def build_nested_cond_function():
    d1 = Block(0, if_instr(cmp_e("t1", 0x1111000011110001), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", 0x2222000022220002), 20, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", 0x3333000033330003), 30, 99, 2))
    chooser = Block(10, if_instr(var("program_cond"), 11, 12, 10))
    nested = Block(11, if_instr(var("nested_cond"), 14, 15, 11))
    false_arm = Block(12, set_var("next_state", const(0x3333000033330003), 12), goto(13))
    join = Block(13, set_var("state", var("next_state"), 13), goto(16))
    true_a = Block(14, set_var("next_state", const(0x2222000022220002), 14), goto(13))
    true_b = Block(15, set_var("next_state", const(0x2222000022220002), 15), goto(13))
    obb2 = Block(20, goto(20))
    obb3 = Block(30, goto(30))
    exit_bb = Block(99, goto(99))
    link(d1, chooser, d2)
    link(d2, obb2, d3)
    link(d3, obb3, exit_bb)
    link(chooser, nested, false_arm)
    link(nested, true_a, true_b)
    link(true_a, join)
    link(true_b, join)
    link(false_arm, join)
    link(join, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 100)],
        "t2": [set_var("t2", var("state"), 101)],
        "t3": [set_var("t3", var("state"), 102)],
        "next_state": [true_a[14], true_b[15], false_arm[12]],
        "state": [join[13]],
    }
    mlil = FakeMlil(
        [d1, d2, d3, chooser, nested, false_arm, join, true_a, true_b, obb2, obb3, exit_bb],
        defs,
    )
    return FakeFunc(mlil), chooser, obb2, obb3


def build_competing_groups_function(main_count=7, small_count=3):
    blocks = []
    defs = {"state": [], "arg": []}
    for i in range(main_count):
        cmp_var = f"m{i}"
        token = 0x1111000011110000 + i
        row_start = i * 10
        hit_start = 1000 + i * 10
        miss_start = 2000 + i * 10
        row = Block(row_start, if_instr(cmp_e(cmp_var, token, size=8), hit_start, miss_start, row_start))
        hit = Block(hit_start, set_var("state", const(token), hit_start), goto(hit_start + 1))
        miss = Block(miss_start, goto(miss_start))
        link(row, hit, miss)
        defs[cmp_var] = [set_var(cmp_var, var("state"), 1000 + i)]
        defs["state"].append(hit[hit_start])
        blocks.extend([row, hit, miss])
    for i in range(small_count):
        cmp_var = f"s{i}"
        row_start = 3000 + i * 10
        hit_start = 4000 + i * 10
        miss_start = 5000 + i * 10
        row = Block(row_start, if_instr(cmp_e(cmp_var, i, size=4), hit_start, miss_start, row_start))
        hit = Block(hit_start, goto(hit_start))
        miss = Block(miss_start, goto(miss_start))
        link(row, hit, miss)
        defs[cmp_var] = [set_var(cmp_var, var("arg"), 1100 + i)]
        blocks.extend([row, hit, miss])
    mlil = FakeMlil(blocks, defs)
    return FakeFunc(mlil)


def build_indirect_predicate_dispatcher():
    tokens = (
        0x1111000011110001,
        0x2222000022220002,
        0x3333000033330003,
    )
    rows = []
    definitions = {"state": []}
    targets = []
    for index, (row_start, target_start, token) in enumerate(
        zip((0, 10, 20), (100, 110, 120), tokens, strict=True)
    ):
        alias_name = f"t{index}"
        predicate_name = f"predicate{index}"
        alias = set_var(alias_name, var("state"), row_start)
        predicate_definition = set_var(
            predicate_name,
            cmp_e(alias_name, token),
            row_start + 1,
        )
        predicate = var(predicate_name)
        predicate.ssa_form = types.SimpleNamespace(src=f"{predicate_name}#1")
        ssa_definition = types.SimpleNamespace(
            instr_index=900 + index,
            non_ssa_form=predicate_definition,
        )
        predicate.function = types.SimpleNamespace(
            ssa_form=types.SimpleNamespace(
                get_ssa_var_definition=(
                    lambda _ssa, definition=ssa_definition: definition
                ),
            ),
        )
        false_target = (10, 20, 130)[index]
        row = Block(
            row_start,
            alias,
            predicate_definition,
            if_instr(predicate, target_start, false_target, row_start + 2),
        )
        target = Block(
            target_start,
            set_var("state", const(tokens[(index + 1) % len(tokens)]), target_start),
            goto(target_start + 1),
        )
        definitions[alias_name] = [alias]
        definitions[predicate_name] = [predicate_definition]
        definitions["state"].append(target[target_start])
        rows.append(row)
        targets.append(target)
    exit_bb = Block(130, goto(130))
    link(rows[0], targets[0], rows[1])
    link(rows[1], targets[1], rows[2])
    link(rows[2], targets[2], exit_bb)
    for target in targets:
        link(target, rows[0])
    mlil = FakeMlil([*rows, *targets, exit_bb], definitions)
    return FakeFunc(mlil), targets[0], targets[1]


def test_compute_redirections_recovers_unconditional_transition_from_dispatcher_cluster():
    func, obb1, obb2 = build_uncond_function()

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        r["kind"] == "uncond"
        and r["obb"] is obb1
        and r["target_bb"] is obb2
        and r["state_token"] == (0x2222000022220002, 8)
        and r["obsolete_state_writes"] == {obb1[10].instr_index}
        for r in redirections
    )


def test_typed_deflatten_plan_reuses_the_atomic_rewrite_backend(monkeypatch):
    func, obb1, obb2 = build_uncond_function()
    raw_plan = next(
        plan
        for plan in compute_redirections(FakeBv(), func)
        if plan["kind"] == "uncond" and plan["obb"] is obb1
    )
    plans = deflatten._typed_plans_from_legacy_redirections((raw_plan,))
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    copied = CopiedMlil()

    def fake_copy(ctx_arg, replacements, mlil=None):
        assert ctx_arg is ctx
        assert mlil is func.mlil
        assert set(replacements) == {obb1[10].instr_index, obb1[11].instr_index}
        copied.outputs[obb1[10].instr_index] = replacements[obb1[10].instr_index](copied, obb1[10])
        copied.outputs[obb1[11].instr_index] = replacements[obb1[11].instr_index](copied, obb1[11])
        return copied, 2

    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        fake_copy,
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(ctx, func.mlil, plans)

    assert new_mlil is copied
    assert applied == 1
    assert copied.outputs == {
        obb1[10].instr_index: ("nop", ("loc", obb1[10].expr_index)),
        obb1[11].instr_index: (
            "goto",
            ("copied-label", obb2.start),
            ("loc", obb1[11].expr_index),
        ),
    }


def test_compute_redirections_recovers_transition_before_shared_state_latch():
    func, obb1, obb2, shared_tail, commit = build_shared_state_latch_function()

    redirections = compute_redirections(FakeBv(), func)

    plan = next(plan for plan in redirections if plan["obb"] is obb1)
    assert plan["target_bb"] is obb2
    assert plan["state_token"] == (0x2222000022220002, 8)
    assert plan["exit_jumps"] == (obb1[11],)
    assert shared_tail[50] not in plan["exit_jumps"]
    assert commit[61] not in plan["exit_jumps"]


@pytest.mark.parametrize("mode", ("side_effect", "commit_call", "field_copy"))
def test_shared_state_latch_rejects_impure_or_inexact_commit(mode):
    func, obb1, _obb2, _shared_tail, _commit = build_shared_state_latch_function(mode)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_shared_state_latch_rejects_copy_defined_after_its_use():
    func, obb1, _obb2, _shared_tail, commit = build_shared_state_latch_function()
    root_copy = set_var("state", var("latch_tmp"), 60)
    late_copy = set_var("latch_tmp", var("next_state"), 61)
    tail = goto(62)
    commit.instructions = [root_copy, late_copy, tail]
    commit.end = commit.start + len(commit.instructions)
    for instruction in commit:
        instruction.il_basic_block = commit
        instruction.function = func.mlil
        func.mlil._by_index[instruction.instr_index] = instruction
    func.mlil._defs["state"] = [root_copy]
    func.mlil._defs["latch_tmp"] = [late_copy]

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_shared_state_latch_rejects_another_dispatcher_root_write():
    func, obb1, _obb2, _shared_tail, _commit = build_shared_state_latch_function()
    extra_write = set_var("state", const(0xDEADBEEF), 70)
    append_block(func.mlil, Block(70, extra_write, goto(71)))
    func.mlil._defs["state"].append(extra_write)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_shared_state_latch_rejects_transition_value_read_outside_latch():
    func, obb1, _obb2, _shared_tail, _commit = build_shared_state_latch_function()
    observer = set_var("observed", var("next_state"), 70)
    append_block(func.mlil, Block(70, observer, goto(71)))
    func.mlil._defs["observed"] = [observer]

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_shared_state_latch_rejects_partial_transition_write():
    func, obb1, _obb2, _shared_tail, _commit = build_shared_state_latch_function()
    partial_write = Expr(
        "MLIL_SET_VAR_FIELD",
        dest="next_state",
        offset=0,
        src=const(0xDEADBEEF),
        instr_index=70,
    )
    append_block(func.mlil, Block(70, partial_write, goto(71)))

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_dispatcher_ne_leaf_uses_false_branch_as_token_target():
    func, obb1, obb2 = build_ne_leaf_function()

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        r["kind"] == "uncond"
        and r["obb"] is obb1
        and r["target_bb"] is obb2
        and r["state_token"] == (0x2222000022220002, 8)
        for r in redirections
    )


def test_compute_redirections_routes_tokens_through_unsigned_range_dispatcher():
    func, obb1, obb2, obb3, obb4 = build_range_dispatch_function()

    redirections = compute_redirections(FakeBv(), func)

    targets = {
        redirection["obb"].start: redirection["target_bb"].start
        for redirection in redirections
        if redirection["kind"] == "uncond" and not redirection.get("entry")
    }
    assert targets == {
        obb1.start: obb2.start,
        obb2.start: obb3.start,
        obb3.start: obb4.start,
        obb4.start: obb1.start,
    }


@pytest.mark.parametrize(
    "op,var_on_left,bound,true_token,false_token",
    [
        ("MLIL_CMP_ULT", True, 0x80, 0x7F, 0x80),
        ("MLIL_CMP_ULE", True, 0x80, 0x80, 0x81),
        ("MLIL_CMP_UGT", True, 0x80, 0x81, 0x80),
        ("MLIL_CMP_UGE", True, 0x80, 0x80, 0x7F),
        ("MLIL_CMP_SLT", True, 0x00, 0xFF, 0x00),
        ("MLIL_CMP_SLE", True, 0x00, 0x00, 0x01),
        ("MLIL_CMP_SGT", True, 0x00, 0x01, 0x00),
        ("MLIL_CMP_SGE", True, 0x00, 0x00, 0xFF),
        ("MLIL_CMP_ULT", False, 0x80, 0x81, 0x80),
        ("MLIL_CMP_ULE", False, 0x80, 0x80, 0x7F),
        ("MLIL_CMP_UGT", False, 0x80, 0x7F, 0x80),
        ("MLIL_CMP_UGE", False, 0x80, 0x80, 0x81),
        ("MLIL_CMP_SLT", False, 0x00, 0x01, 0x00),
        ("MLIL_CMP_SLE", False, 0x00, 0x00, 0xFF),
        ("MLIL_CMP_SGT", False, 0x00, 0xFF, 0x00),
        ("MLIL_CMP_SGE", False, 0x00, 0x00, 0x01),
    ],
)
def test_compute_redirections_preserves_mlil_range_comparison_semantics(
    op,
    var_on_left,
    bound,
    true_token,
    false_token,
):
    func, true_leaf, false_leaf = build_comparison_dispatch_function(
        op,
        var_on_left,
        bound,
        true_token,
        false_token,
    )

    redirections = compute_redirections(FakeBv(), func)

    targets = {
        redirection["obb"].start: redirection["target_bb"].start
        for redirection in redirections
        if redirection["kind"] == "uncond"
    }
    assert targets[true_leaf.start] == true_leaf.start
    assert targets[false_leaf.start] == false_leaf.start


def test_compute_redirections_rejects_transition_with_unresolved_state_write():
    func, obb1, _obb2 = build_uncond_function()
    jump = obb1.instructions[-1]
    unresolved = set_var("state", var("unknown"), jump.instr_index)
    unresolved.il_basic_block = obb1
    unresolved.function = func.mlil
    jump.instr_index += 1
    obb1.instructions.insert(-1, unresolved)
    obb1.end += 1
    func.mlil._by_index[unresolved.instr_index] = unresolved
    func.mlil._by_index[jump.instr_index] = jump

    redirections = compute_redirections(FakeBv(), func)

    assert all(redirection["obb"] is not obb1 for redirection in redirections)


def test_compute_redirections_rejects_state_write_with_unresolved_definition_arm():
    func, obb1, _obb2 = build_uncond_function()
    obb1[10].src = var("choice")
    known = set_var("choice", const(0x2222000022220002), 150)
    unknown = set_var("choice", var("unknown"), 151)
    known.il_basic_block = obb1
    unknown.il_basic_block = obb1
    func.mlil._defs["choice"] = [known, unknown]

    redirections = compute_redirections(FakeBv(), func)

    assert all(redirection["obb"] is not obb1 for redirection in redirections)


def test_compute_redirections_rejects_route_through_unsupported_state_comparison():
    func, obb1, _obb2, _obb3, _obb4 = build_range_dispatch_function()
    d1, d2 = func.mlil.basic_blocks[:2]
    old_edge = next(edge for edge in d1.outgoing_edges if edge.target is d2)
    d1.outgoing_edges.remove(old_edge)
    d2.incoming_edges.remove(old_edge)
    barrier = Block(
        5,
        if_instr(cmp_range("MLIL_CMP_BOGUS", "t1", 0x2500), d2.start, d2.start, 5),
    )
    d1[0].false = barrier.start
    link(d1, barrier)
    link(barrier, d2)
    func.mlil.basic_blocks.append(barrier)
    barrier.il_function = func.mlil
    for ins in barrier:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins

    redirections = compute_redirections(FakeBv(), func)

    assert all(redirection["obb"] is not obb1 for redirection in redirections)


def test_compute_redirections_keeps_every_dispatcher_exit_in_one_transition():
    func, head, true_jump, false_jump, target = build_multi_exit_function()

    redirections = compute_redirections(FakeBv(), func)

    plan = next(redirection for redirection in redirections if redirection["obb"] is head)
    assert plan["target_bb"] is target
    assert set(plan["exit_jumps"]) == {true_jump, false_jump}


def test_compute_redirections_rejects_exits_that_route_same_token_differently():
    func, head, _true_jump, false_jump, _target = build_multi_exit_function()
    false_tail = false_jump.il_basic_block
    old_edge = false_tail.outgoing_edges[0]
    old_edge.target.incoming_edges.remove(old_edge)
    false_tail.outgoing_edges.remove(old_edge)
    d3 = next(bb for bb in func.mlil.basic_blocks if bb.start == 2)
    link(false_tail, d3)

    redirections = compute_redirections(FakeBv(), func)

    assert all(redirection["obb"] is not head for redirection in redirections)


def test_compute_redirections_keeps_every_entry_dispatcher_exit():
    func, entry, true_jump, false_jump, target = build_multi_exit_entry_function()

    redirections = compute_redirections(FakeBv(), func)

    plan = next(redirection for redirection in redirections if redirection.get("entry"))
    assert plan["obb"] is entry
    assert plan["target_bb"] is target
    assert set(plan["exit_jumps"]) == {true_jump, false_jump}


def test_entry_transition_rejects_external_entry_into_intermediate_region():
    func, _entry, _true_jump, _false_jump, _target = build_multi_exit_entry_function()
    dispatcher = next(bb for bb in func.mlil.basic_blocks if bb.start == 0)
    true_tail = next(bb for bb in func.mlil.basic_blocks if bb.start == 210)
    old_edge = true_tail.outgoing_edges.pop()
    old_edge.target.incoming_edges.remove(old_edge)
    middle = Block(230, set_var("middle_value", const(1), 230), goto(231))
    exit_tail = Block(240, set_var("exit_value", const(2), 240), goto(241))
    external = Block(250, goto(250))
    link(true_tail, middle)
    link(middle, exit_tail)
    link(exit_tail, dispatcher)
    link(external, middle)
    for block in (middle, exit_tail, external):
        block.il_function = func.mlil
        for ins in block:
            ins.function = func.mlil
            func.mlil._by_index[ins.instr_index] = ins
        func.mlil.basic_blocks.append(block)

    redirections = compute_redirections(FakeBv(), func)

    assert all(not plan.get("entry") for plan in redirections)


def test_cleanup_uncertainty_keeps_state_write_without_losing_redirection():
    func, obb1, obb2 = build_uncond_function()
    state_reader = Block(80, set_var("observed", var("state"), 80), goto(81))
    state_reader.il_function = func.mlil
    for ins in state_reader:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(state_reader)

    redirections = compute_redirections(FakeBv(), func)

    plan = next(redirection for redirection in redirections if redirection["obb"] is obb1)
    assert plan["target_bb"] is obb2
    assert plan["obsolete_state_writes"] == set()


def test_redirection_rejects_dispatcher_temporary_observed_by_target():
    func, obb1, obb2 = build_uncond_function()
    state_write, tail = obb2.instructions
    observed = set_var("observed", var("t2"), state_write.instr_index)
    observed.il_basic_block = obb2
    observed.function = func.mlil
    state_write.instr_index += 1
    tail.instr_index += 1
    obb2.instructions.insert(0, observed)
    obb2.end += 1
    func.mlil._by_index[observed.instr_index] = observed
    func.mlil._by_index[state_write.instr_index] = state_write
    func.mlil._by_index[tail.instr_index] = tail

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_cleanup_plan_does_not_match_unrelated_write_with_same_token():
    func, obb1, _obb2 = build_uncond_function()
    unrelated = Block(
        80,
        set_var("unrelated", const(0x2222000022220002), 80),
        goto(81),
    )
    unrelated.il_function = func.mlil
    for ins in unrelated:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(unrelated)

    redirections = compute_redirections(FakeBv(), func)

    plan = next(redirection for redirection in redirections if redirection["obb"] is obb1)
    assert plan["obsolete_state_writes"] == {obb1[10].instr_index}


def test_compute_redirections_ignores_stray_equality_compare():
    func, obb1, obb2 = build_uncond_function()
    stray = Block(80, if_instr(cmp_e("arg0", 0, size=4), 99, 99, 80))
    stray.il_function = func.mlil
    for instr in stray:
        instr.function = func.mlil
        func.mlil._by_index[instr.instr_index] = instr
    func.mlil.basic_blocks.append(stray)

    redirections = compute_redirections(FakeBv(), func)

    assert any(r["kind"] == "uncond" and r["obb"] is obb1 and r["target_bb"] is obb2 for r in redirections)


def test_dispatcher_analysis_rejects_even_a_smaller_competing_candidate_group():
    func = build_competing_groups_function()

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_dispatcher_analysis_rejects_close_candidate_groups():
    func = build_competing_groups_function(main_count=6, small_count=3)

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_dispatcher_boundary_rejects_unrelated_assignment():
    block = Block(
        500,
        set_var("important", const(1), 500),
        goto(501),
    )
    mlil = FakeMlil([block], {"important": [block[500]]})

    assert not deflatten._router_boundary_block(
        mlil,
        block,
        "state",
        {"state"},
    )


def test_dispatcher_boundary_rejects_unobserved_pure_state_arithmetic():
    derived = set_var(
        "dead_result",
        Expr("MLIL_ADD", left=var("state"), right=const(1)),
        500,
    )
    block = Block(500, derived, goto(501))
    mlil = FakeMlil([block], {"dead_result": [derived]})

    assert not deflatten._router_boundary_block(
        mlil,
        block,
        "state",
        {"state"},
    )


def test_dispatcher_boundary_rejects_observed_arithmetic_result():
    derived = set_var(
        "observed_result",
        Expr("MLIL_ADD", left=var("state"), right=const(1)),
        500,
    )
    block = Block(500, derived, goto(501))
    observer = set_var("sink", var("observed_result"), 600)
    mlil = FakeMlil(
        [block, Block(600, observer, goto(601))],
        {"observed_result": [derived], "sink": [observer]},
    )

    assert not deflatten._router_boundary_block(
        mlil,
        block,
        "state",
        {"state"},
    )


def test_dispatcher_boundary_rejects_state_dependent_load():
    derived = set_var(
        "dead_result",
        Expr(
            "MLIL_ADD",
            left=var("state"),
            right=Expr("MLIL_LOAD", src=var("state")),
        ),
        500,
    )
    block = Block(500, derived, goto(501))
    mlil = FakeMlil([block], {"dead_result": [derived]})

    assert not deflatten._router_boundary_block(
        mlil,
        block,
        "state",
        {"state"},
    )


def test_dispatcher_analysis_does_not_hide_impure_non_dominant_comparison_row():
    func, _obb1, _obb2 = build_uncond_function()
    alias = set_var("extra_state", var("state"), 500)
    side_effect = Expr(
        "MLIL_CALL",
        dest=const(0x5000),
        params=[],
        instr_index=501,
    )
    extra_row = Block(
        500,
        alias,
        side_effect,
        if_instr(cmp_e("extra_state", 0x1111, size=4), 10, 99, 502),
    )
    extra_row.il_function = func.mlil
    for instruction in extra_row:
        instruction.function = func.mlil
        func.mlil._by_index[instruction.instr_index] = instruction
    func.mlil._defs["extra_state"] = [alias]
    func.mlil.basic_blocks.append(extra_row)

    analysis = deflatten._analyze_dispatcher(func.mlil)

    assert analysis is not None
    assert extra_row.start not in analysis["dispatcher_starts"]


def test_dispatcher_analysis_rejects_alias_defined_only_on_one_predecessor():
    func, _obb1, _obb2 = build_uncond_function()
    alias_definition = func.mlil._defs["t1"][0]
    alias_definition.instr_index = 80
    external = Block(80, alias_definition, goto(81))
    dispatcher = next(bb for bb in func.mlil.basic_blocks if bb.start == 0)
    link(external, dispatcher)
    external.il_function = func.mlil
    for ins in external:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(external)

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_dispatcher_row_local_copy_overrides_external_alias_history():
    func, obb1, obb2 = build_uncond_function()
    external = Block(80, set_var("t1", const(0xDEADBEEF), 80), goto(81))
    external.il_function = func.mlil
    for ins in external:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(external)
    func.mlil._defs["t1"].append(external[80])

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        plan["kind"] == "uncond"
        and plan["obb"] is obb1
        and plan["target_bb"] is obb2
        for plan in redirections
    )


def test_dispatcher_rejects_field_reads_as_full_state_copies():
    func, _obb1, _obb2 = build_uncond_function()
    for alias in ("t1", "t2", "t3"):
        func.mlil._defs[alias][0].src = Expr(
            "MLIL_VAR_FIELD",
            src="state",
            offset=4,
        )

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_dispatcher_rejects_alias_copy_after_resolved_comparison():
    func, _obb1, _obb2 = build_uncond_function()
    dispatcher = next(bb for bb in func.mlil.basic_blocks if bb.start == 0)
    if_il = dispatcher.instructions[-1]
    predicate_definition = set_var("predicate", if_il.condition, -2)
    predicate_definition.il_basic_block = dispatcher
    predicate_definition.function = func.mlil
    func.mlil._by_index[-2] = predicate_definition
    predicate = var("predicate")
    predicate.ssa_form = types.SimpleNamespace(src="predicate#1")
    predicate.function = types.SimpleNamespace(
        ssa_form=types.SimpleNamespace(
            get_ssa_var_definition=lambda _ssa: predicate_definition,
        ),
    )
    if_il.condition = predicate

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_dispatcher_accepts_current_row_indirect_predicate_definition():
    func, first_target, second_target = build_indirect_predicate_dispatcher()

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        plan["kind"] == "uncond"
        and plan["obb"] is first_target
        and plan["target_bb"] is second_target
        for plan in redirections
    )


def test_dispatcher_rejects_resolved_comparison_defined_in_predecessor():
    func, _obb1, _obb2 = build_uncond_function()
    dispatcher = next(bb for bb in func.mlil.basic_blocks if bb.start == 0)
    if_il = dispatcher.instructions[-1]
    predicate_definition = set_var("predicate", if_il.condition, 80)
    predecessor = Block(80, predicate_definition, goto(81))
    link(predecessor, dispatcher)
    predecessor.il_function = func.mlil
    for ins in predecessor:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(predecessor)
    predicate = var("predicate")
    predicate.ssa_form = types.SimpleNamespace(src="predicate#1")
    predicate.function = types.SimpleNamespace(
        ssa_form=types.SimpleNamespace(
            get_ssa_var_definition=lambda _ssa: predicate_definition,
        ),
    )
    if_il.condition = predicate

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_unconditional_transition_rejects_state_field_mutation():
    func, obb1, _obb2 = build_uncond_function()
    tail = obb1.instructions[-1]
    field_write = Expr(
        "MLIL_SET_VAR_FIELD",
        dest="state",
        offset=0,
        src=const(0x3333000033330003),
        instr_index=tail.instr_index,
    )
    field_write.il_basic_block = obb1
    field_write.function = func.mlil
    tail.instr_index += 1
    obb1.instructions.insert(-1, field_write)
    obb1.end += 1
    func.mlil._by_index[field_write.instr_index] = field_write
    func.mlil._by_index[tail.instr_index] = tail

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_partial_state_write_in_successor_prevents_predecessor_cleanup():
    func, obb1, obb2 = build_uncond_function()
    successor_write = obb2.instructions[0]
    successor_write.operation = MediumLevelILOperation.MLIL_SET_VAR_FIELD
    successor_write.offset = 0

    redirections = compute_redirections(FakeBv(), func)

    predecessor = next(plan for plan in redirections if plan["obb"] is obb1)
    assert predecessor["target_bb"] is obb2
    assert predecessor["obsolete_state_writes"] == set()


def test_split_state_read_in_successor_prevents_predecessor_cleanup():
    func, obb1, obb2 = build_uncond_function()
    state_write, tail = obb2.instructions
    split = Expr(
        "MLIL_VAR_SPLIT",
        high="state",
        low="other",
        vars_read=("state", "other"),
    )
    observed = set_var("observed", split, state_write.instr_index)
    call = Expr(
        "MLIL_CALL",
        dest=const(0x5000),
        params=[var("observed")],
        instr_index=state_write.instr_index + 1,
    )
    state_write.instr_index += 2
    tail.instr_index += 2
    for instruction in (observed, call, state_write, tail):
        instruction.il_basic_block = obb2
        instruction.function = func.mlil
        func.mlil._by_index[instruction.instr_index] = instruction
    obb2.instructions = [observed, call, state_write, tail]
    obb2.end += 2
    func.mlil._defs["observed"] = [observed]

    redirections = compute_redirections(FakeBv(), func)

    predecessor = next(plan for plan in redirections if plan["obb"] is obb1)
    assert predecessor["obsolete_state_writes"] == set()


def test_unconditional_transition_rejects_state_address_passed_to_call():
    func, obb1, _obb2 = build_uncond_function()
    tail = obb1.instructions[-1]
    state_pointer = address_of_field("state", 0)
    call = Expr(
        "MLIL_CALL",
        dest=const(0x5000),
        params=[state_pointer],
        instr_index=tail.instr_index,
    )
    call.il_basic_block = obb1
    call.function = func.mlil
    tail.instr_index += 1
    obb1.instructions.insert(-1, call)
    obb1.end += 1
    func.mlil._by_index[call.instr_index] = call
    func.mlil._by_index[tail.instr_index] = tail

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


@pytest.mark.parametrize("operation", ["MLIL_UNIMPL", "MLIL_UNIMPL_MEM"])
@pytest.mark.parametrize("nested", [False, True])
def test_unconditional_transition_rejects_unmodeled_instruction(operation, nested):
    func, obb1, _obb2 = build_uncond_function()
    tail = obb1.instructions[-1]
    unmodeled_expr = Expr(operation)
    unmodeled = (
        set_var("unmodeled_result", unmodeled_expr, tail.instr_index)
        if nested
        else unmodeled_expr
    )
    unmodeled.instr_index = tail.instr_index
    unmodeled.il_basic_block = obb1
    unmodeled.function = func.mlil
    tail.instr_index += 1
    obb1.instructions.insert(-1, unmodeled)
    obb1.end += 1
    func.mlil._by_index[unmodeled.instr_index] = unmodeled
    func.mlil._by_index[tail.instr_index] = tail
    if nested:
        func.mlil._defs["unmodeled_result"] = [unmodeled]

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_unconditional_transition_rejects_state_field_pointer_passed_to_call():
    func, obb1, _obb2 = build_uncond_function()
    tail = obb1.instructions[-1]
    address = address_of_field("state", 0)
    holder_write = Expr(
        "MLIL_SET_VAR_FIELD",
        dest="holder",
        offset=0,
        src=address,
        instr_index=tail.instr_index,
    )
    holder_read = Expr("MLIL_VAR_FIELD", src="holder", offset=0)
    call = Expr(
        "MLIL_CALL",
        dest=const(0x5000),
        params=[holder_read],
        instr_index=tail.instr_index + 1,
    )
    tail.instr_index += 2
    for instruction in (holder_write, call, tail):
        instruction.il_basic_block = obb1
        instruction.function = func.mlil
        func.mlil._by_index[instruction.instr_index] = instruction
    obb1.instructions[-1:-1] = [holder_write, call]
    obb1.end += 2
    func.mlil._defs["holder"] = [holder_write]

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_unconditional_transition_rejects_call_after_state_address_escaped_to_memory():
    func, obb1, _obb2 = build_uncond_function()
    tail = obb1.instructions[-1]
    escaped_address = address_of_field("state", 0)
    escape = Expr(
        "MLIL_STORE",
        dest=const(0x7000),
        src=escaped_address,
        instr_index=tail.instr_index,
    )
    call = Expr(
        "MLIL_CALL",
        dest=const(0x5000),
        params=[],
        instr_index=tail.instr_index + 1,
    )
    tail.instr_index += 2
    for instruction in (escape, call, tail):
        instruction.il_basic_block = obb1
        instruction.function = func.mlil
        func.mlil._by_index[instruction.instr_index] = instruction
    obb1.instructions[-1:-1] = [escape, call]
    obb1.end += 2

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


@pytest.mark.parametrize(
    "mutate_op",
    ["MLIL_CALL", "MLIL_UNIMPL_MEM", "MLIL_TRAP", "MLIL_BP"],
)
def test_unconditional_transition_rejects_unknown_effect_after_retained_state_address(
    mutate_op,
):
    func, obb1, obb2 = build_uncond_function()
    register_tail = obb2.instructions[-1]
    register = Expr(
        "MLIL_CALL",
        dest=const(0x5000),
        params=[address_of_field("state", 0)],
        instr_index=register_tail.instr_index,
    )
    register_tail.instr_index += 1
    obb2.instructions.insert(-1, register)
    obb2.end += 1

    mutate_tail = obb1.instructions[-1]
    mutate = Expr(mutate_op, instr_index=mutate_tail.instr_index)
    if mutate_op == "MLIL_CALL":
        mutate.dest = const(0x6000)
        mutate.params = []
    mutate_tail.instr_index += 1
    obb1.instructions.insert(-1, mutate)
    obb1.end += 1

    for block, instructions in (
        (obb2, (register, register_tail)),
        (obb1, (mutate, mutate_tail)),
    ):
        for instruction in instructions:
            instruction.il_basic_block = block
            instruction.function = func.mlil
            func.mlil._by_index[instruction.instr_index] = instruction

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


@pytest.mark.parametrize("hidden_source_kind", ["token", "address"])
def test_unconditional_transition_rejects_store_through_reloaded_state_address(
    hidden_source_kind,
):
    func, obb1, _obb2 = build_uncond_function()
    tail = obb1.instructions[-1]
    slot = const(0x9000)
    publish = Expr(
        "MLIL_STORE",
        dest=slot,
        src=address_of_field("state", 0),
        instr_index=tail.instr_index,
    )
    loaded_pointer = Expr("MLIL_LOAD", src=const(0x9000), size=8)
    pointer_definition = set_var(
        "escaped_pointer",
        loaded_pointer,
        tail.instr_index + 1,
    )
    hidden_state_write = Expr(
        "MLIL_STORE",
        dest=var("escaped_pointer"),
        src=(
            const(0x3333000033330003)
            if hidden_source_kind == "token"
            else address_of_field("state", 0)
        ),
        instr_index=tail.instr_index + 2,
    )
    tail.instr_index += 3
    for instruction in (publish, pointer_definition, hidden_state_write, tail):
        instruction.il_basic_block = obb1
        instruction.function = func.mlil
        func.mlil._by_index[instruction.instr_index] = instruction
    obb1.instructions[-1:-1] = [publish, pointer_definition, hidden_state_write]
    obb1.end += 3
    func.mlil._defs["escaped_pointer"] = [pointer_definition]

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not obb1 for plan in redirections)


def test_compute_redirections_recovers_entry_state_transition():
    func, entry, obb1 = build_entry_function()

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        r["kind"] == "uncond"
        and r.get("entry") is True
        and r["obb"] is entry
        and r["exit_jumps"] == (entry[91],)
        and r["target_bb"] is obb1
        and r["state_token"] == (0x1111000011110001, 8)
        for r in redirections
    )


def test_compute_redirections_recovers_conditional_two_branch_transition():
    func, chooser, obb2, obb3 = build_cond_function()

    redirections = compute_redirections(FakeBv(), func)
    cond = next(r for r in redirections if r["kind"] == "if_else")

    assert cond["obb"] is chooser
    assert cond["if_il"] is chooser[10]
    assert cond["true_target"] is obb2
    assert cond["false_target"] is obb3
    assert cond["true_token"] == (0x2222000022220002, 8)
    assert cond["false_token"] == (0x3333000033330003, 8)
    assert cond["obsolete_state_writes"] == {13}
    assert cond["obsolete_state_write_witnesses"] == {13: func.mlil[13]}
    assert 13 not in deflatten._analyze_dispatcher(func.mlil)["dispatcher_starts"]


def test_compute_redirections_preserves_shared_semantic_tail():
    func, chooser, join, obb2, obb3 = build_cond_function_with_shared_semantic_tail()

    redirections = compute_redirections(FakeBv(), func)
    cond = next(r for r in redirections if r.get("obb") is chooser)

    assert cond["rewrite_mode"] == "shared_exit"
    assert cond["shared_exit"] is join[15]
    assert cond["state_var"] == "state"
    assert cond["true_target"] is obb2
    assert cond["false_target"] is obb3
    assert cond["obsolete_state_writes"] == set()


def test_compute_redirections_replays_an_unchanged_shared_predicate():
    func, chooser, _join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)

    redirections = compute_redirections(FakeBv(), func)
    plan = next(item for item in redirections if item.get("obb") is chooser)

    assert plan["rewrite_mode"] == "shared_exit"
    assert plan["shared_condition"] is chooser[10]
    assert plan["shared_condition_witness"] == (
        MediumLevelILOperation.MLIL_CMP_E,
        "guard",
        (1, 8),
        True,
    )
    # The state write is intentionally retained for shared exits even when the
    # original predicate can be replayed: that region may carry semantic work.
    assert plan["obsolete_state_writes"] == set()


def test_compute_redirections_does_not_replay_a_shared_predicate_after_shared_write():
    func, chooser, join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    write = join[14]
    write.dest = "guard"
    write.src = const(0)
    write.vars_written = ["guard"]
    func.mlil._defs["guard"] = [write]

    redirections = compute_redirections(FakeBv(), func)
    plan = next(item for item in redirections if item.get("obb") is chooser)

    assert plan["rewrite_mode"] == "shared_exit"
    assert plan["shared_condition"] is None


@pytest.mark.parametrize("address_op", ["MLIL_ADDRESS_OF", "MLIL_ADDRESS_OF_FIELD"])
def test_shared_predicate_replay_rejects_address_taken_before_source_if(address_op):
    address = set_var("guard_holder", Expr(address_op, src="guard", offset=4), 0)
    source = if_instr(cmp_e("guard", 1), 1, 2, 1)
    Block(0, address, source)

    assert deflatten._addresses_variable_before_if(source, "guard")


@pytest.mark.parametrize("address_op", ["MLIL_ADDRESS_OF", "MLIL_ADDRESS_OF_FIELD"])
def test_shared_predicate_replay_rejects_predicate_address_escape(address_op):
    func, chooser, join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    join[14].dest = "guard_holder"
    join[14].src = Expr(address_op, src="guard", offset=4)
    join[14].vars_written = ["guard_holder"]

    redirections = compute_redirections(FakeBv(), func)
    plan = next(item for item in redirections if item.get("obb") is chooser)

    assert plan["rewrite_mode"] == "shared_exit"
    assert plan["shared_condition"] is None


def test_compute_redirections_preserves_arm_selected_shared_semantics():
    func, chooser, join, obb2, obb3 = build_cond_function_with_arm_selected_shared_semantics()

    redirections = compute_redirections(FakeBv(), func)
    cond = next(r for r in redirections if r.get("obb") is chooser)

    assert cond["rewrite_mode"] == "shared_exit"
    assert cond["shared_exit"] is join[32]
    assert cond["true_target"] is obb2
    assert cond["false_target"] is obb3
    assert cond["obsolete_state_writes"] == set()


def test_compute_redirections_preserves_unrelated_arm_write_before_shared_exit():
    func, chooser, _obb2, _obb3 = build_cond_function()
    true_arm = next(bb for bb in func.mlil.basic_blocks if bb.start == 11)
    other_write = set_var("other_state", const(0x4444000044440004), 111)
    other_write.il_basic_block = true_arm
    other_write.function = func.mlil
    true_arm.instructions.insert(-1, other_write)
    true_arm.end += 1
    func.mlil._by_index[other_write.instr_index] = other_write
    func.mlil._defs["other_state"] = [other_write]

    redirections = compute_redirections(FakeBv(), func)
    plan = next(item for item in redirections if item.get("if_il") is chooser[10])

    assert plan["rewrite_mode"] == "shared_exit"
    assert plan["obsolete_state_writes"] == set()


def test_conditional_rejects_arm_with_external_entry():
    func, chooser, _obb2, _obb3 = build_cond_function()
    true_arm = next(bb for bb in func.mlil.basic_blocks if bb.start == 11)
    external = Block(200, goto(200))
    link(external, true_arm)
    external.il_function = func.mlil
    for ins in external:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(external)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan.get("if_il") is not chooser[10] for plan in redirections)


def test_conditional_shared_exit_rejects_merge_with_external_entry():
    func, chooser, join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    external = Block(200, goto(200))
    link(external, join)
    append_block(func.mlil, external)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan.get("if_il") is not chooser[10] for plan in redirections)


def test_conditional_rewrite_rejects_state_observed_outside_dispatcher():
    func, chooser, obb2, _obb3 = build_cond_function()
    tail = obb2.instructions[-1]
    observed = set_var("observed", var("state"), tail.instr_index)
    observed.il_basic_block = obb2
    observed.function = func.mlil
    tail.instr_index += 1
    obb2.instructions.insert(-1, observed)
    obb2.end += 1
    func.mlil._by_index[observed.instr_index] = observed
    func.mlil._by_index[tail.instr_index] = tail

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan.get("if_il") is not chooser[10] for plan in redirections)


def test_conditional_rewrite_rejects_state_field_address_escape():
    func, chooser, obb2, _obb3 = build_cond_function()
    tail = obb2.instructions[-1]
    observed = set_var("observed", address_of_field("state", 4), tail.instr_index)
    observed.il_basic_block = obb2
    observed.function = func.mlil
    tail.instr_index += 1
    obb2.instructions.insert(-1, observed)
    obb2.end += 1
    func.mlil._by_index[observed.instr_index] = observed
    func.mlil._by_index[tail.instr_index] = tail

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan.get("if_il") is not chooser[10] for plan in redirections)


def test_conditional_uses_arm_exits_when_state_writes_can_be_preserved():
    func, head, true_jump, false_jump, obb2 = build_multi_exit_function()
    false_write = false_jump.il_basic_block.instructions[0]
    false_write.src = const(0x3333000033330003)
    obb3 = next(bb for bb in func.mlil.basic_blocks if bb.start == 40)

    redirections = compute_redirections(FakeBv(), func)

    plan = next(plan for plan in redirections if plan["obb"] is head)
    assert plan["rewrite_mode"] == "arm_exits"
    assert plan["exit_targets"] == ((true_jump, obb2), (false_jump, obb3))


def test_conditional_arm_exit_rewrite_rejects_external_entry():
    func, head, _true_jump, false_jump, _obb2 = build_multi_exit_function()
    false_jump.il_basic_block.instructions[0].src = const(0x3333000033330003)
    true_tail = next(bb for bb in func.mlil.basic_blocks if bb.start == 11)
    external = Block(200, goto(200))
    link(external, true_tail)
    external.il_function = func.mlil
    for ins in external:
        ins.function = func.mlil
        func.mlil._by_index[ins.instr_index] = ins
    func.mlil.basic_blocks.append(external)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not head for plan in redirections)


def test_transition_rejects_path_that_reaches_dispatcher_without_state_write():
    func, head, _true_jump, false_jump, _target = build_multi_exit_function()
    false_write = false_jump.il_basic_block.instructions[0]
    false_write.operation = MediumLevelILOperation.MLIL_NOP
    func.mlil._defs["state"].remove(false_write)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan["obb"] is not head for plan in redirections)


def test_compute_redirections_rejects_conditional_arm_with_looping_exit_path():
    func, chooser, _obb2, _obb3 = build_cond_function()
    join = next(bb for bb in func.mlil.basic_blocks if bb.start == 13)
    dispatcher = next(bb for bb in func.mlil.basic_blocks if bb.start == 0)
    old_jump = join.instructions[-1]
    loop_or_dispatch = if_instr(
        var("loop_or_dispatch"),
        dispatcher.start,
        join.start,
        old_jump.instr_index,
    )
    loop_or_dispatch.il_basic_block = join
    loop_or_dispatch.function = func.mlil
    join.instructions[-1] = loop_or_dispatch
    func.mlil._by_index[loop_or_dispatch.instr_index] = loop_or_dispatch
    old_edge = join.outgoing_edges.pop()
    old_edge.target.incoming_edges.remove(old_edge)
    link(join, dispatcher)
    link(join, join)

    redirections = compute_redirections(FakeBv(), func)

    assert all(plan.get("if_il") is not chooser[10] for plan in redirections)


def test_compute_redirections_rejects_ambiguous_conditional_candidates():
    token_a = 0x1111000011110001
    token_b = 0x2222000022220002
    token_c = 0x3333000033330003
    d1 = Block(0, if_instr(cmp_e("t1", token_a), 10, 1, 0))
    d2 = Block(1, if_instr(cmp_e("t2", token_b), 100, 2, 1))
    d3 = Block(2, if_instr(cmp_e("t3", token_c), 110, 120, 2))
    head = Block(10, if_instr(var("gate"), 20, 30, 10))
    chooser_a = Block(20, if_instr(var("cond_a"), 40, 50, 20))
    chooser_b = Block(30, if_instr(var("cond_b"), 60, 70, 30))
    a_true = Block(40, set_var("state", const(token_b), 40), goto(41))
    a_false = Block(50, set_var("state", const(token_c), 50), goto(51))
    b_true = Block(60, set_var("state", const(token_b), 60), goto(61))
    b_false = Block(70, set_var("state", const(token_c), 70), goto(71))
    real_b = Block(100, goto(100))
    real_c = Block(110, goto(110))
    exit_bb = Block(120, goto(120))
    link(d1, head, d2)
    link(d2, real_b, d3)
    link(d3, real_c, exit_bb)
    link(head, chooser_a, chooser_b)
    link(chooser_a, a_true, a_false)
    link(chooser_b, b_true, b_false)
    for arm in (a_true, a_false, b_true, b_false):
        link(arm, d1)
    defs = {
        "t1": [set_var("t1", var("state"), 200)],
        "t2": [set_var("t2", var("state"), 201)],
        "t3": [set_var("t3", var("state"), 202)],
        "state": [a_true[40], a_false[50], b_true[60], b_false[70]],
    }
    mlil = FakeMlil(
        [
            d1,
            d2,
            d3,
            head,
            chooser_a,
            chooser_b,
            a_true,
            a_false,
            b_true,
            b_false,
            real_b,
            real_c,
            exit_bb,
        ],
        defs,
    )

    redirections = compute_redirections(FakeBv(), FakeFunc(mlil))

    assert all(
        plan["kind"] != "if_else" or plan["obb"] is not head
        for plan in redirections
    )


def test_compute_redirections_allows_nested_pure_condition_in_branch_tail():
    func, chooser, obb2, obb3 = build_nested_cond_function()

    redirections = compute_redirections(FakeBv(), func)
    cond = next(r for r in redirections if r["kind"] == "if_else")

    assert cond["obb"] is chooser
    assert cond["if_il"] is chooser[10]
    assert cond["true_target"] is obb2
    assert cond["false_target"] is obb3
    assert cond["true_token"] == (0x2222000022220002, 8)
    assert cond["false_token"] == (0x3333000033330003, 8)


def test_rewrite_redirections_emits_copied_label_edges(monkeypatch):
    assert not hasattr(deflatten, "apply_redirections_il")
    func, chooser, obb2, obb3 = build_cond_function()
    uncond_jump = goto(200)
    source = Block(200, uncond_jump)
    uncond_jump.function = func.mlil
    func.mlil._by_index[200] = uncond_jump
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    copied = CopiedMlil()

    def fake_copy(ctx_arg, replacements, mlil=None):
        assert ctx_arg is ctx
        assert mlil is func.mlil
        assert set(replacements) == {
            uncond_jump.instr_index,
            chooser[10].instr_index,
            13,
        }
        copied.outputs[uncond_jump.instr_index] = replacements[uncond_jump.instr_index](copied, uncond_jump)
        copied.outputs[chooser[10].instr_index] = replacements[chooser[10].instr_index](copied, chooser[10])
        copied.outputs[13] = replacements[13](copied, func.mlil[13])
        return copied, 3

    monkeypatch.setattr(deflatten, "copy_mlil_with_instruction_rewrites", fake_copy, raising=False)

    new_mlil, applied = rewrite_redirections_mlil(
        ctx,
        func.mlil,
        [
            {
                "kind": "uncond",
                "exit_jumps": (uncond_jump,),
                "target_bb": obb2,
                "obb": source,
                "obsolete_state_writes": set(),
            },
            {
                "kind": "if_else",
                "rewrite_mode": "condition",
                "if_il": chooser[10],
                "true_target": obb2,
                "false_target": obb3,
                "obb": chooser,
                "obsolete_state_writes": {13},
                "obsolete_state_write_witnesses": {13: func.mlil[13]},
            },
        ],
    )

    assert new_mlil is copied
    assert applied == 2
    assert copied.outputs == {
        uncond_jump.instr_index: (
            "goto",
            ("copied-label", obb2.start),
            ("loc", uncond_jump.expr_index),
        ),
        chooser[10].instr_index: (
            "if",
            ("copy", chooser[10].condition),
            ("copied-label", obb2.start),
            ("copied-label", obb3.start),
            ("loc", chooser[10].expr_index),
        ),
        13: ("nop", ("loc", func.mlil[13].expr_index)),
    }


def test_rewrite_conditional_arm_exits_preserves_original_if(monkeypatch):
    func, chooser, obb2, obb3 = build_cond_function()
    true_jump = goto(200)
    false_jump = goto(201)
    true_source = Block(200, true_jump)
    false_source = Block(201, false_jump)
    for block in (true_source, false_source):
        block.il_function = func.mlil
        for instruction in block:
            instruction.function = func.mlil
            func.mlil._by_index[instruction.instr_index] = instruction
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    copied = CopiedMlil()

    def fake_copy(ctx_arg, replacements, mlil=None):
        assert ctx_arg is ctx
        assert mlil is func.mlil
        assert set(replacements) == {200, 201}
        assert chooser[10].instr_index not in replacements
        copied.outputs[200] = replacements[200](copied, true_jump)
        copied.outputs[201] = replacements[201](copied, false_jump)
        return copied, 2

    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        fake_copy,
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(
        ctx,
        func.mlil,
        [
            {
                "kind": "if_else",
                "rewrite_mode": "arm_exits",
                "if_il": chooser[10],
                "true_target": obb2,
                "false_target": obb3,
                "exit_targets": ((true_jump, obb2), (false_jump, obb3)),
                "obb": chooser,
                "obsolete_state_writes": set(),
            },
        ],
    )

    assert new_mlil is copied
    assert applied == 1
    assert copied.outputs == {
        200: ("goto", ("copied-label", obb2.start), ("loc", true_jump.expr_index)),
        201: ("goto", ("copied-label", obb3.start), ("loc", false_jump.expr_index)),
    }


def test_rewrite_conditional_shared_exit_preserves_semantic_tail(monkeypatch):
    func, chooser, join, obb2, obb3 = build_cond_function_with_shared_semantic_tail()
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    copied = CopiedMlil()

    def fake_copy(ctx_arg, replacements, mlil=None):
        assert ctx_arg is ctx
        assert mlil is func.mlil
        assert set(replacements) == {join[15].instr_index}
        copied.outputs[join[15].instr_index] = replacements[join[15].instr_index](
            copied,
            join[15],
        )
        return copied, 1

    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        fake_copy,
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(ctx, func.mlil, [plan])

    loc = ("loc", join[15].expr_index)
    assert new_mlil is copied
    assert applied == 1
    assert copied.outputs == {
        join[15].instr_index: (
            "if",
            (
                "cmp_e",
                8,
                ("var", 8, "state", loc),
                ("const", 8, 0x2222000022220002, loc),
                loc,
            ),
            ("copied-label", obb2.start),
            ("copied-label", obb3.start),
            loc,
        ),
    }


def test_rewrite_conditional_shared_exit_replays_original_predicate(monkeypatch):
    func, chooser, join, obb2, obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    copied = CopiedMlil()

    def fake_copy(ctx_arg, replacements, mlil=None):
        assert ctx_arg is ctx
        assert mlil is func.mlil
        copied.outputs[join[15].instr_index] = replacements[join[15].instr_index](
            copied,
            join[15],
        )
        return copied, 1

    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        fake_copy,
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(ctx, func.mlil, [plan])

    loc = ("loc", join[15].expr_index)
    assert new_mlil is copied
    assert applied == 1
    assert copied.outputs == {
        join[15].instr_index: (
            "if",
            ("copy", chooser[10].condition),
            ("copied-label", obb2.start),
            ("copied-label", obb3.start),
            loc,
        ),
    }


def test_rewrite_shared_exit_rejects_stale_replayed_condition(monkeypatch):
    func, chooser, _join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    other, other_chooser, _other_join, _other_obb2, _other_obb3 = (
        build_cond_function_with_shared_semantic_tail()
    )
    other_chooser[10].condition = cmp_e("guard", 1)
    plan["shared_condition"] = other_chooser[10]
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("stale condition reached copy backend"),
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [plan],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_shared_exit_accepts_an_equivalent_bn_instruction_wrapper(monkeypatch):
    func, chooser, _join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    # Binary Ninja may materialize a distinct Python wrapper for the same IL
    # instruction. Exact IL coordinates and operands, not wrapper identity,
    # bind the replayed predicate to the source IF.
    plan["shared_condition"] = copy(chooser[10])
    copied = CopiedMlil()
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: (copied, 1),
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [plan],
    )

    assert new_mlil is copied
    assert applied == 1


def test_rewrite_shared_exit_rejects_different_current_if_with_same_predicate(monkeypatch):
    func, chooser, _join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    other = Block(400, if_instr(cmp_e("guard", 1), 20, 30, 400))
    append_block(func.mlil, other)
    plan["shared_condition"] = other[400]
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("unbound condition reached copy backend"),
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [plan],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_shared_exit_rejects_missing_predicate_witness(monkeypatch):
    func, chooser, _join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    chooser[10].condition = cmp_e("guard", 1)
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    plan["shared_condition_witness"] = None
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("unwitnessed condition reached copy backend"),
        raising=False,
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [plan],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_conditional_shared_exit_rejects_token_outside_width(monkeypatch):
    func, chooser, _join, _obb2, _obb3 = build_cond_function_with_shared_semantic_tail()
    plan = next(
        item
        for item in compute_redirections(FakeBv(), func)
        if item.get("obb") is chooser
    )
    plan["true_token"] = (0x100, 1)
    plan["false_token"] = (0, 1)
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("out-of-width token reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [plan],
    )

    assert new_mlil is None
    assert applied == 0


@pytest.mark.parametrize("rewrite_mode", [None, "unknown"])
def test_rewrite_conditional_rejects_missing_or_unknown_mode(
    monkeypatch,
    rewrite_mode,
):
    func, chooser, obb2, obb3 = build_cond_function()
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    plan = {
        "kind": "if_else",
        "if_il": chooser[10],
        "true_target": obb2,
        "false_target": obb3,
        "obb": chooser,
        "obsolete_state_writes": set(),
    }
    if rewrite_mode is not None:
        plan["rewrite_mode"] = rewrite_mode
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("invalid plan reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(ctx, func.mlil, [plan])

    assert new_mlil is None
    assert applied == 0


def test_rewrite_rejects_stale_source_object_at_current_instruction_index(
    monkeypatch,
):
    func, obb1, obb2 = build_uncond_function()
    stale_jump = goto(obb1[10].instr_index)
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("stale plan reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        ctx,
        func.mlil,
        [
            {
                "kind": "uncond",
                "exit_jumps": (stale_jump,),
                "target_bb": obb2,
                "obb": obb1,
                "obsolete_state_writes": set(),
            },
        ],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_rejects_source_owned_by_another_mlil(monkeypatch):
    func, obb1, obb2 = build_uncond_function()
    current = obb1.instructions[-1]
    source = types.SimpleNamespace(
        operation=current.operation,
        instr_index=current.instr_index,
        expr_index=current.expr_index,
        address=current.address,
        dest=current.dest,
        function=object(),
    )
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("foreign-owner plan reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "uncond",
            "exit_jumps": (source,),
            "target_bb": obb2,
            "obb": obb1,
            "obsolete_state_writes": set(),
        }],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_rejects_stale_goto_destination(monkeypatch):
    func, obb1, obb2 = build_uncond_function()
    current = obb1.instructions[-1]
    source = types.SimpleNamespace(
        operation=current.operation,
        instr_index=current.instr_index,
        expr_index=current.expr_index,
        address=current.address,
        dest=current.dest + 1,
        function=func.mlil,
    )
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("stale GOTO plan reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "uncond",
            "exit_jumps": (source,),
            "target_bb": obb2,
            "obb": obb1,
            "obsolete_state_writes": set(),
        }],
    )

    assert new_mlil is None
    assert applied == 0


@pytest.mark.parametrize("field", ["condition", "true", "false"])
def test_rewrite_rejects_stale_if_operands(monkeypatch, field):
    func, chooser, obb2, obb3 = build_cond_function()
    current = chooser[10]
    source = types.SimpleNamespace(
        operation=current.operation,
        instr_index=current.instr_index,
        expr_index=current.expr_index,
        address=current.address,
        condition=current.condition,
        true=current.true,
        false=current.false,
        function=func.mlil,
    )
    if field == "condition":
        source.condition = types.SimpleNamespace(
            operation=current.condition.operation,
            expr_index=current.condition.expr_index + 1,
            size=current.condition.size,
        )
    else:
        setattr(source, field, getattr(source, field) + 1)
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("stale IF plan reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "if_else",
            "rewrite_mode": "condition",
            "if_il": source,
            "true_target": obb2,
            "false_target": obb3,
            "obb": chooser,
            "obsolete_state_writes": set(),
        }],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_rejects_stale_cleanup_write_operand(monkeypatch):
    func, chooser, obb2, obb3 = build_cond_function()
    current = func.mlil[13]
    recorded = types.SimpleNamespace(
        operation=current.operation,
        instr_index=current.instr_index,
        expr_index=current.expr_index,
        address=current.address,
        dest="stale_state",
        src=current.src,
        size=current.size,
        function=func.mlil,
    )
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail(
            "stale cleanup witness reached copy backend"
        ),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "if_else",
            "rewrite_mode": "condition",
            "if_il": chooser[10],
            "true_target": obb2,
            "false_target": obb3,
            "obb": chooser,
            "obsolete_state_writes": {13},
            "obsolete_state_write_witnesses": {13: recorded},
        }],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_rejects_duplicate_plan_owned_cleanup_witness(monkeypatch):
    func, chooser, obb2, obb3 = build_cond_function()
    uncond_jump = goto(200)
    source = Block(200, uncond_jump)
    uncond_jump.function = func.mlil
    func.mlil._by_index[200] = uncond_jump
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail(
            "duplicate cleanup witness reached copy backend"
        ),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [
            {
                "kind": "uncond",
                "exit_jumps": (uncond_jump,),
                "target_bb": obb2,
                "obb": source,
                "obsolete_state_writes": {13},
                "obsolete_state_write_witnesses": {13: func.mlil[13]},
            },
            {
                "kind": "if_else",
                "rewrite_mode": "condition",
                "if_il": chooser[10],
                "true_target": obb2,
                "false_target": obb3,
                "obb": chooser,
                "obsolete_state_writes": {13},
                "obsolete_state_write_witnesses": {13: func.mlil[13]},
            },
        ],
    )

    assert new_mlil is None
    assert applied == 0


@pytest.mark.parametrize("field", ["instr_index", "expr_index", "address"])
def test_rewrite_rejects_non_exact_numeric_source_witness(monkeypatch, field):
    func, obb1, obb2 = build_uncond_function()
    current = obb1.instructions[-1]
    source = types.SimpleNamespace(
        operation=current.operation,
        instr_index=current.instr_index,
        expr_index=current.expr_index,
        address=current.address,
    )
    setattr(source, field, float(getattr(source, field)))
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("malformed source reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "uncond",
            "exit_jumps": (source,),
            "target_bb": obb2,
            "obb": obb1,
            "obsolete_state_writes": set(),
        }],
    )

    assert new_mlil is None
    assert applied == 0


@pytest.mark.parametrize("field", ["instr_index", "expr_index", "address"])
def test_rewrite_rejects_non_exact_numeric_current_source(monkeypatch, field):
    func, obb1, obb2 = build_uncond_function()
    current = obb1.instructions[-1]
    source = types.SimpleNamespace(
        operation=current.operation,
        instr_index=current.instr_index,
        expr_index=current.expr_index,
        address=current.address,
    )
    setattr(current, field, float(getattr(current, field)))
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("malformed current source reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "uncond",
            "exit_jumps": (source,),
            "target_bb": obb2,
            "obb": obb1,
            "obsolete_state_writes": set(),
        }],
    )

    assert new_mlil is None
    assert applied == 0


def test_rewrite_rejects_arm_exit_without_exact_instruction_index(monkeypatch):
    func, chooser, obb2, obb3 = build_cond_function()
    malformed_jump = types.SimpleNamespace(operation=MediumLevelILOperation.MLIL_GOTO)
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("malformed arm exit reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [{
            "kind": "if_else",
            "rewrite_mode": "arm_exits",
            "exit_targets": ((malformed_jump, obb2), (obb3.instructions[-1], obb3)),
            "obb": chooser,
            "obsolete_state_writes": set(),
        }],
    )

    assert new_mlil is None
    assert applied == 0


@pytest.mark.parametrize("invalid_index", [False, -1])
def test_rewrite_rejects_boolean_or_negative_cleanup_index(
    monkeypatch,
    invalid_index,
):
    func, obb1, obb2 = build_uncond_function()
    func.mlil._by_index[invalid_index] = obb1[10]
    monkeypatch.setattr(
        deflatten,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: pytest.fail("invalid cleanup reached copy backend"),
    )

    new_mlil, applied = rewrite_redirections_mlil(
        types.SimpleNamespace(mlil=func.mlil, llil="context-llil"),
        func.mlil,
        [
            {
                "kind": "uncond",
                "exit_jumps": (obb1.instructions[-1],),
                "target_bb": obb2,
                "obb": obb1,
                "obsolete_state_writes": {invalid_index},
            },
        ],
    )

    assert new_mlil is None
    assert applied == 0


@pytest.mark.parametrize("invalid_start", [False, -1])
def test_replacement_plans_reject_boolean_or_negative_target_starts(invalid_start):
    func, chooser, obb2, _obb3 = build_cond_function()
    invalid_target = types.SimpleNamespace(start=invalid_start)
    jump = obb2.instructions[-1]

    assert deflatten._replacements_for_redirection({
        "kind": "uncond",
        "exit_jumps": (jump,),
        "target_bb": invalid_target,
    }) is None
    assert deflatten._replacements_for_redirection({
        "kind": "if_else",
        "rewrite_mode": "condition",
        "if_il": chooser[10],
        "true_target": invalid_target,
        "false_target": obb2,
    }) is None
    assert deflatten._replacements_for_redirection({
        "kind": "if_else",
        "rewrite_mode": "arm_exits",
        "exit_targets": ((jump, invalid_target),),
    }) is None


def test_rewrite_redirections_rejects_a_partial_multi_redirection_batch(monkeypatch):
    func, chooser, obb2, obb3 = build_cond_function()
    uncond_jump = goto(200)
    source = Block(200, uncond_jump)
    uncond_jump.function = func.mlil
    func.mlil._by_index[200] = uncond_jump
    ctx = types.SimpleNamespace(mlil=func.mlil, llil="context-llil")

    def reject(ctx_arg, replacements, mlil=None):
        assert ctx_arg is ctx
        assert mlil is func.mlil
        assert set(replacements) == {
            uncond_jump.instr_index,
            chooser[10].instr_index,
            13,
        }
        return "partial", 2

    monkeypatch.setattr(deflatten, "copy_mlil_with_instruction_rewrites", reject, raising=False)

    new_mlil, applied = rewrite_redirections_mlil(
        ctx,
        func.mlil,
        [
            {
                "kind": "uncond",
                "exit_jumps": (uncond_jump,),
                "target_bb": obb2,
                "obb": source,
                "obsolete_state_writes": set(),
            },
            {
                "kind": "if_else",
                "rewrite_mode": "condition",
                "if_il": chooser[10],
                "true_target": obb2,
                "false_target": obb3,
                "obb": chooser,
                "obsolete_state_writes": {13},
                "obsolete_state_write_witnesses": {13: func.mlil[13]},
            },
        ],
    )

    assert new_mlil is None
    assert applied == 0
    assert func.mlil.replacements == []


if __name__ == "__main__":
    test_compute_redirections_recovers_unconditional_transition_from_dispatcher_cluster()
    test_dispatcher_ne_leaf_uses_false_branch_as_token_target()
    test_compute_redirections_ignores_stray_equality_compare()
    test_dispatcher_analysis_rejects_even_a_smaller_competing_candidate_group()
    test_dispatcher_analysis_rejects_close_candidate_groups()
    test_compute_redirections_recovers_entry_state_transition()
    test_compute_redirections_recovers_conditional_two_branch_transition()
    test_compute_redirections_allows_nested_pure_condition_in_branch_tail()
    test_rewrite_redirections_emits_copied_label_edges()
    test_rewrite_redirections_rejects_a_partial_multi_redirection_batch()
