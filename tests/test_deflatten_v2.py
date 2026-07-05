import types

from conftest import load_plugin_module


deflatten = load_plugin_module("plugins.DispatchThis.passes.medium.deflatten")

compute_redirections = deflatten.compute_redirections
apply_redirections_il = deflatten.apply_redirections_il


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    _next_index = 1

    def __init__(self, op, **attrs):
        self.operation = Op(op)
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

    def __iter__(self):
        return iter(self.basic_blocks)

    def __getitem__(self, index):
        return self._by_index[index]

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


class FakeFunc:
    def __init__(self, mlil):
        self.medium_level_il = mlil
        self.mlil = mlil
        self.start = 0x4000


class FakeBv:
    pass


def var(name):
    return Expr("MLIL_VAR", src=name)


def const(value, size=8):
    return Expr("MLIL_CONST", constant=value, size=size)


def cmp_e(name, value, size=8):
    return Expr("MLIL_CMP_E", left=var(name), right=const(value, size), size=1)


def cmp_ne(name, value, size=8):
    return Expr("MLIL_CMP_NE", left=var(name), right=const(value, size), size=1)


def set_var(name, src, index):
    ins = Expr("MLIL_SET_VAR", dest=name, src=src, instr_index=index)
    ins.vars_written = [name]
    return ins


def goto(index):
    return Expr("MLIL_GOTO", instr_index=index)


def if_instr(cond, true_index, false_index, index):
    return Expr("MLIL_IF", condition=cond, true=true_index, false=false_index, instr_index=index)


def link(source, *targets):
    for target in targets:
        edge = Edge(source, target)
        source.outgoing_edges.append(edge)
        target.incoming_edges.append(edge)


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


def test_compute_redirections_recovers_unconditional_transition_from_dispatcher_cluster():
    func, obb1, obb2 = build_uncond_function()

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        r["kind"] == "uncond"
        and r["obb"] is obb1
        and r["target_bb"] is obb2
        and r["state_token"] == (0x2222000022220002, 8)
        for r in redirections
    )


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


def test_dispatcher_analysis_ignores_much_smaller_candidate_group():
    func = build_competing_groups_function()

    analysis = deflatten._analyze_dispatcher(func.mlil)

    assert analysis is not None
    assert str(analysis["state_var"]) == "state"


def test_dispatcher_analysis_rejects_close_candidate_groups():
    func = build_competing_groups_function(main_count=6, small_count=3)

    assert deflatten._analyze_dispatcher(func.mlil) is None


def test_compute_redirections_recovers_entry_state_transition():
    func, entry, obb1 = build_entry_function()

    redirections = compute_redirections(FakeBv(), func)

    assert any(
        r["kind"] == "uncond"
        and r.get("entry") is True
        and r["obb"] is entry
        and r["jump"] is entry[91]
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


def test_apply_redirections_rewrites_unconditional_and_conditional_transitions():
    func, chooser, obb2, obb3 = build_cond_function()
    uncond_jump = goto(200)
    source = Block(200, uncond_jump)
    func.mlil._by_index[200] = uncond_jump

    applied = apply_redirections_il(
        func.mlil,
        [
            {"kind": "uncond", "jump": uncond_jump, "target_bb": obb2, "obb": source},
            {
                "kind": "if_else",
                "if_il": chooser[10],
                "true_target": obb2,
                "false_target": obb3,
                "obb": chooser,
            },
        ],
    )

    assert applied == 2
    assert func.mlil.replacements == [
        (uncond_jump.expr_index, ("goto", obb2.start, ("loc", uncond_jump.expr_index))),
        (
            chooser[10].expr_index,
            ("if", ("copy", chooser[10].condition), obb2.start, obb3.start, ("loc", chooser[10].expr_index)),
        ),
    ]
    assert func.mlil.finalized is True
    assert func.mlil.ssa_generated is True


if __name__ == "__main__":
    test_compute_redirections_recovers_unconditional_transition_from_dispatcher_cluster()
    test_dispatcher_ne_leaf_uses_false_branch_as_token_target()
    test_compute_redirections_ignores_stray_equality_compare()
    test_dispatcher_analysis_ignores_much_smaller_candidate_group()
    test_dispatcher_analysis_rejects_close_candidate_groups()
    test_compute_redirections_recovers_entry_state_transition()
    test_compute_redirections_recovers_conditional_two_branch_transition()
    test_compute_redirections_allows_nested_pure_condition_in_branch_tail()
    test_apply_redirections_rewrites_unconditional_and_conditional_transitions()
