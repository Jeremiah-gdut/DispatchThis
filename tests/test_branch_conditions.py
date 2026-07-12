import types

from binaryninja import MediumLevelILOperation

from conftest import load_plugin_module


branch_conditions = load_plugin_module("plugins.DispatchThis.passes.medium.branch_conditions")


class Expr:
    _next_index = 1

    def __init__(self, op, children=(), **attrs):
        self.operation = MediumLevelILOperation[op]
        self.children = list(children)
        self.expr_index = Expr._next_index
        Expr._next_index += 1
        for key, value in attrs.items():
            setattr(self, key, value)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out

    @property
    def value(self):
        raise AttributeError


class FakeMLIL:
    def __init__(self, defs=None):
        self.defs = defs or {}

    def get_var_definitions(self, var):
        return self.defs.get(var, [])


def const(value, size=8):
    return Expr("MLIL_CONST", constant=value, size=size)


def cmp_ne(left, right):
    return Expr("MLIL_CMP_NE", [left, right], left=left, right=right)


def bool_to_int(cond):
    return Expr("MLIL_BOOL_TO_INT", [cond], src=cond)


def load(src):
    return Expr("MLIL_LOAD", [src], src=src, size=8)


def var(name):
    return Expr("MLIL_VAR", src=name)


def set_var(src):
    return Expr("MLIL_SET_VAR", [src], src=src)


def neg(src):
    return Expr("MLIL_NEG", [src], src=src)


def mul(left, right):
    return Expr("MLIL_MUL", [left, right], left=left, right=right)


def lsl(left, right):
    return Expr("MLIL_LSL", [left, right], left=left, right=right)


def add(left, right):
    return Expr("MLIL_ADD", [left, right], left=left, right=right)


def low_part(src, size):
    return Expr("MLIL_LOW_PART", [src], src=src, size=size)


def test_bool_to_int_jump_maps_true_and_false_targets():
    cond = cmp_ne(const(1), const(0))
    dest = add(const(0x1000), lsl(bool_to_int(cond), const(6)))
    jump = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        targets={0x1000: 10, 0x1040: 20},
        dest=dest,
    )

    plan = branch_conditions._plan_for_jump(None, FakeMLIL(), jump)

    assert plan["condition"] is cond
    assert plan["true"] == 20
    assert plan["false"] == 10


def test_bool_to_int_jump_rejects_predicate_recovered_from_earlier_load():
    cond = cmp_ne(load(const(0x2000)), const(0))
    selector = "selector"
    jump = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        targets={0x1000: 10, 0x1040: 20},
        dest=add(const(0x1000), lsl(var(selector), const(6))),
        il_basic_block=types.SimpleNamespace(incoming_edges=()),
    )
    mlil = FakeMLIL({selector: [set_var(bool_to_int(cond))]})

    assert branch_conditions._plan_for_jump(None, mlil, jump) is None


def test_jump_target_eval_handles_negated_multiply():
    idx = "idx"
    dest = add(const(0x1040), mul(var(idx), neg(const(0x20))))
    jump = types.SimpleNamespace(
        targets={0x1040: 10, 0x1000: 20},
        dest=dest,
    )

    assert branch_conditions._target_for_value(None, FakeMLIL(), jump, idx, 0) == 10
    assert branch_conditions._target_for_value(None, FakeMLIL(), jump, idx, 2) == 20


def test_target_mapping_rejects_low_48_bit_address_collision():
    jump = types.SimpleNamespace(
        targets={
            0x1000: 10,
            (1 << 48) | 0x1000: 20,
        },
    )

    assert branch_conditions._unique_target_index(jump, 0x1000) is None


def test_target_eval_honors_narrow_casts_and_struct_load_offsets():
    narrowed = low_part(const(0x10100), 1)
    assert branch_conditions._eval_const(None, FakeMLIL(), narrowed, {}) == 0

    class Bv:
        @staticmethod
        def read(addr, size):
            return (0x1234).to_bytes(size, "little") if addr == 0x1020 else b""

    loaded = Expr(
        "MLIL_LOAD_STRUCT",
        [const(0x1000)],
        src=const(0x1000),
        offset=0x20,
        size=8,
    )
    assert branch_conditions._eval_const(Bv(), FakeMLIL(), loaded, {}) == 0x1234


def test_eval_const_accepts_multiple_defs_when_they_agree():
    mlil = FakeMLIL({"base": [set_var(const(0x1000)), set_var(const(0x1000))]})

    assert branch_conditions._eval_const(None, mlil, add(var("base"), const(0x40)), {}) == 0x1040


def test_assigned_target_var_plan_does_not_require_same_source_if():
    true_assign = types.SimpleNamespace(src=const(0), size=8, instr_index=11)
    false_assign = types.SimpleNamespace(src=const(1), size=8, instr_index=12)
    jump = types.SimpleNamespace(
        targets={0x1000: 10, 0x1040: 20},
        dest=add(const(0x1000), lsl(var("idx"), const(6))),
    )

    plan = branch_conditions._plan_for_assigned_target_var(
        None,
        FakeMLIL(),
        jump,
        {"idx": (0, true_assign)},
        {"idx": (1, false_assign)},
    )

    assert plan["condition_var"] == "idx"
    assert plan["condition_value"] == 0
    assert plan["true"] == 10
    assert plan["false"] == 20
    assert 11 not in plan["cleanup_roots"]
    assert 12 not in plan["cleanup_roots"]


def test_assigned_target_var_rejects_conflicting_valid_witnesses(monkeypatch):
    first = object()
    second = object()
    true_assigns = {
        first: (0, types.SimpleNamespace(size=8)),
        second: (0, types.SimpleNamespace(size=8)),
    }
    false_assigns = {
        first: (1, types.SimpleNamespace(size=8)),
        second: (1, types.SimpleNamespace(size=8)),
    }
    mappings = {
        first: {0: 10, 1: 20},
        second: {0: 20, 1: 10},
    }
    monkeypatch.setattr(
        branch_conditions,
        "_target_for_value",
        lambda _bv, _mlil, _jump, variable, value: mappings[variable][value],
    )

    plan = branch_conditions._plan_for_assigned_target_var(
        None,
        FakeMLIL(),
        types.SimpleNamespace(dest=const(0)),
        true_assigns,
        false_assigns,
    )

    assert plan is None


def test_assigned_target_var_accepts_only_after_all_witnesses_agree(monkeypatch):
    first = object()
    second = object()
    true_assigns = {
        first: (0, types.SimpleNamespace(size=8)),
        second: (7, types.SimpleNamespace(size=4)),
    }
    false_assigns = {
        first: (1, types.SimpleNamespace(size=8)),
        second: (9, types.SimpleNamespace(size=4)),
    }
    calls = []

    def target_for_value(_bv, _mlil, _jump, variable, value):
        calls.append((variable, value))
        return 10 if value in {0, 7} else 20

    monkeypatch.setattr(branch_conditions, "_target_for_value", target_for_value)

    plan = branch_conditions._plan_for_assigned_target_var(
        None,
        FakeMLIL(),
        types.SimpleNamespace(dest=const(0)),
        true_assigns,
        false_assigns,
    )

    assert plan["condition_var"] is first
    assert (plan["true"], plan["false"]) == (10, 20)
    assert calls == [(first, 0), (first, 1), (second, 7), (second, 9)]


def test_source_if_requires_the_arm_to_have_one_ingress():
    source_if = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_IF,
        true=10,
        false=20,
    )
    source = types.SimpleNamespace(end=1)
    foreign = types.SimpleNamespace(end=2)
    arm = types.SimpleNamespace(
        start=10,
        incoming_edges=(
            types.SimpleNamespace(source=source),
            types.SimpleNamespace(source=foreign),
        ),
    )

    class Mlil:
        def __getitem__(self, index):
            return source_if if index == 0 else types.SimpleNamespace(
                operation=MediumLevelILOperation.MLIL_GOTO,
            )

    assert branch_conditions._source_if_for_arm(Mlil(), arm) is None


def test_source_condition_preserves_saved_predicate_and_rejects_recomputation():
    predicate = var("predicate")
    definition = set_var(cmp_ne(var("state"), const(7)))
    mlil = FakeMLIL({"predicate": [definition]})

    assert branch_conditions._condition_expr(
        mlil,
        types.SimpleNamespace(condition=predicate),
    ) is predicate
    assert branch_conditions._condition_expr(
        mlil,
        types.SimpleNamespace(condition=cmp_ne(var("state"), const(7))),
    ) is None


def test_const_assigns_keeps_only_the_final_whole_variable_write():
    initial = Expr(
        "MLIL_SET_VAR",
        [const(0)],
        dest="idx",
        src=const(0),
        vars_written=["idx"],
    )
    overwrite = Expr(
        "MLIL_SET_VAR",
        [var("runtime")],
        dest="idx",
        src=var("runtime"),
        vars_written=["idx"],
    )

    class Mlil:
        def __getitem__(self, index):
            return (initial, overwrite)[index]

    assert branch_conditions._const_assigns(
        Mlil(),
        types.SimpleNamespace(start=0, end=2),
    ) == {}


def test_source_condition_rejects_arm_that_redefines_predicate():
    predicate = "predicate"
    write = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_SET_VAR,
        vars_written=[predicate],
    )

    assert not branch_conditions._arms_preserve_variable(([write], []), predicate)


def test_join_write_rejects_stale_selector_candidates(monkeypatch):
    selector = "selector"

    class Block(list):
        pass

    true_arm = Block()
    true_arm.start = 10
    false_arm = Block()
    false_arm.start = 20
    source_if = types.SimpleNamespace(
        expr_index=50,
        condition=var("predicate"),
        true=10,
        false=20,
    )
    true_assign = types.SimpleNamespace(src=const(0), size=8, instr_index=11)
    false_assign = types.SimpleNamespace(src=const(1), size=8, instr_index=12)
    jump = Expr(
        "MLIL_JUMP_TO",
        dest=add(const(0x1000), lsl(var(selector), const(6))),
        targets={0x1000: 30, 0x1040: 40},
        instr_index=31,
    )
    selector_overwrite = Expr(
        "MLIL_SET_VAR_ALIASED_FIELD",
        dest=selector,
        vars_written=[],
        instr_index=30,
    )

    join = Block((selector_overwrite, jump))
    join.incoming_edges = (
        types.SimpleNamespace(source=true_arm),
        types.SimpleNamespace(source=false_arm),
    )
    jump.il_basic_block = join

    class Mlil(FakeMLIL):
        def __getitem__(self, index):
            return types.SimpleNamespace(
                il_basic_block=true_arm if index == 10 else false_arm,
            )

    assigns = {
        id(true_arm): {selector: (0, true_assign)},
        id(false_arm): {selector: (1, false_assign)},
    }
    seen_candidates = []
    monkeypatch.setattr(
        branch_conditions,
        "_const_assigns",
        lambda _mlil, arm: assigns[id(arm)],
    )
    monkeypatch.setattr(branch_conditions, "_source_if_for_arm", lambda *_args: source_if)
    monkeypatch.setattr(branch_conditions, "set_roots_before_instruction", lambda *_args: set())
    monkeypatch.setattr(branch_conditions, "variable_address_escapes", lambda *_args: False)

    def owned_source(_mlil, _jump, _true, _false, candidates):
        seen_candidates.extend(candidates)
        return (source_if, (30, 40)) if candidates else None

    monkeypatch.setattr(branch_conditions, "_owned_decode_source", owned_source)

    assert branch_conditions._plan_for_jump(None, Mlil(), jump) is None
    assert seen_candidates == []


def test_join_write_to_predicate_uses_selector_fallback(monkeypatch):
    selector = "selector"
    predicate = "predicate"

    class Block(list):
        pass

    true_arm = Block()
    true_arm.start = 10
    false_arm = Block()
    false_arm.start = 20
    source_if = types.SimpleNamespace(
        expr_index=50,
        condition=var(predicate),
        true=10,
        false=20,
    )
    true_assign = types.SimpleNamespace(src=const(0), size=8, instr_index=11)
    false_assign = types.SimpleNamespace(src=const(1), size=8, instr_index=12)
    jump = Expr(
        "MLIL_JUMP_TO",
        dest=add(const(0x1000), lsl(var(selector), const(6))),
        targets={0x1000: 30, 0x1040: 40},
        instr_index=31,
    )
    predicate_overwrite = Expr(
        "MLIL_SET_VAR",
        dest=predicate,
        vars_written=[predicate],
        instr_index=30,
    )

    join = Block((predicate_overwrite, jump))
    join.incoming_edges = (
        types.SimpleNamespace(source=true_arm),
        types.SimpleNamespace(source=false_arm),
    )
    jump.il_basic_block = join

    class Mlil(FakeMLIL):
        def __getitem__(self, index):
            return types.SimpleNamespace(
                il_basic_block=true_arm if index == 10 else false_arm,
            )

    assigns = {
        id(true_arm): {selector: (0, true_assign)},
        id(false_arm): {selector: (1, false_assign)},
    }
    monkeypatch.setattr(
        branch_conditions,
        "_const_assigns",
        lambda _mlil, arm: assigns[id(arm)],
    )
    monkeypatch.setattr(branch_conditions, "_source_if_for_arm", lambda *_args: source_if)
    monkeypatch.setattr(branch_conditions, "_owned_decode_source", lambda *_args: None)
    monkeypatch.setattr(branch_conditions, "variable_address_escapes", lambda *_args: False)

    plan = branch_conditions._plan_for_jump(None, Mlil(), jump)

    assert "condition" not in plan
    assert plan["condition_var"] == selector


def test_owned_decode_source_requires_a_private_side_effect_free_diamond(monkeypatch):
    class Block:
        def __init__(self, start, *instructions):
            self.start = start
            self.end = start + len(instructions)
            self.instructions = list(instructions)
            self.incoming_edges = []
            self.outgoing_edges = []
            for instruction in instructions:
                instruction.il_basic_block = self

        def __iter__(self):
            return iter(self.instructions)

    def link(source, target):
        edge = types.SimpleNamespace(source=source, target=target)
        source.outgoing_edges.append(edge)
        target.incoming_edges.append(edge)

    source_if = Expr(
        "MLIL_IF",
        condition=var("condition"),
        true=1,
        false=3,
        instr_index=0,
    )
    true_value = const(0)
    true_assign = Expr(
        "MLIL_SET_VAR",
        [true_value],
        src=true_value,
        dest="selector",
        size=8,
        instr_index=1,
    )
    false_value = const(1)
    false_assign = Expr(
        "MLIL_SET_VAR",
        [false_value],
        src=false_value,
        dest="selector",
        size=8,
        instr_index=3,
    )
    decoded_value = add(var("selector"), const(0))
    decoded_value.size = 8
    decoded = Expr(
        "MLIL_SET_VAR",
        [decoded_value],
        src=decoded_value,
        dest="decoded",
        size=8,
        instr_index=5,
    )
    jump = Expr(
        "MLIL_JUMP_TO",
        dest=var("decoded"),
        targets={0x1000: 8, 0x2000: 9},
        instr_index=6,
    )
    source = Block(0, source_if)
    true_arm = Block(
        1,
        true_assign,
        Expr("MLIL_GOTO", instr_index=2),
    )
    false_arm = Block(
        3,
        false_assign,
        Expr("MLIL_GOTO", instr_index=4),
    )
    join = Block(5, decoded, jump)
    target_a = Block(8, Expr("MLIL_NOP", instr_index=8))
    target_b = Block(9, Expr("MLIL_NOP", instr_index=9))
    link(source, true_arm)
    link(source, false_arm)
    link(true_arm, join)
    link(false_arm, join)
    link(join, target_a)
    link(join, target_b)

    instructions = {
        instruction.instr_index: instruction
        for block in (source, true_arm, false_arm, join, target_a, target_b)
        for instruction in block
    }

    class Mlil:
        def __getitem__(self, index):
            return instructions[index]

    monkeypatch.setattr(
        branch_conditions,
        "dependency_variables",
        lambda *_args: {"selector", "decoded"},
    )
    monkeypatch.setattr(branch_conditions, "variables_are_scope_local", lambda *_args: True)
    monkeypatch.setattr(branch_conditions, "variable_address_escapes", lambda *_args: False)
    candidates = [{"true": 8, "false": 9}]

    assert branch_conditions._owned_decode_source(
        Mlil(),
        jump,
        true_arm,
        false_arm,
        candidates,
    ) == (source_if, (8, 9))

    side_effect = Expr("MLIL_CALL", instr_index=10)
    side_effect.il_basic_block = true_arm
    true_arm.instructions.insert(0, side_effect)
    true_arm.end += 1

    assert branch_conditions._owned_decode_source(
        Mlil(),
        jump,
        true_arm,
        false_arm,
        candidates,
    ) is None


def test_owned_source_if_is_redirected_without_moving_its_condition(monkeypatch):
    true_assign = types.SimpleNamespace(src=const(0), size=8, instr_index=11)
    false_assign = types.SimpleNamespace(src=const(1), size=8, instr_index=12)
    source_if = types.SimpleNamespace(
        expr_index=50,
        condition=var("program_condition"),
        true=10,
        false=20,
    )
    true_arm = types.SimpleNamespace(start=10)
    false_arm = types.SimpleNamespace(start=20)
    jump = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        instr_index=30,
        targets={0x1000: 30, 0x1040: 40},
        dest=add(const(0x1000), lsl(var("idx"), const(6))),
    )

    class Join(list):
        pass

    predicate_overwrite = Expr(
        "MLIL_SET_VAR",
        dest="program_condition",
        vars_written=["program_condition"],
        instr_index=29,
    )
    join = Join((predicate_overwrite, jump))
    join.incoming_edges = (
        types.SimpleNamespace(source=true_arm),
        types.SimpleNamespace(source=false_arm),
    )
    jump.il_basic_block = join

    class Mlil(FakeMLIL):
        def __getitem__(self, index):
            return types.SimpleNamespace(
                il_basic_block=true_arm if index == 10 else false_arm,
            )

    assigns = {
        id(true_arm): {"idx": (0, true_assign)},
        id(false_arm): {"idx": (1, false_assign)},
    }
    monkeypatch.setattr(
        branch_conditions,
        "_const_assigns",
        lambda _mlil, arm: assigns[id(arm)],
    )
    monkeypatch.setattr(
        branch_conditions,
        "_source_if_for_arm",
        lambda _mlil, _arm: source_if,
    )
    monkeypatch.setattr(
        branch_conditions,
        "set_roots_before_instruction",
        lambda _mlil, instruction: {41, 42} if instruction is source_if else set(),
    )
    monkeypatch.setattr(
        branch_conditions,
        "_owned_decode_source",
        lambda _mlil, _jump, _true, _false, _candidates: (source_if, (30, 40)),
    )

    plan = branch_conditions._plan_for_jump(None, Mlil(), jump)

    assert plan["rewrite_il"] is source_if
    assert plan["cleanup_roots"] == {11, 12, 41, 42}

    monkeypatch.setattr(
        branch_conditions,
        "variable_address_escapes",
        lambda _mlil, _variable: True,
    )
    escaped_plan = branch_conditions._plan_for_jump(None, Mlil(), jump)
    assert escaped_plan["rewrite_il"] is source_if
    assert escaped_plan["condition_from_rewrite"] is True
    assert escaped_plan["cleanup_roots"] == {11, 12, 41, 42}


def test_translate_branch_conditions_builds_copy_rewrite(monkeypatch):
    jump = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        instr_index=7,
        expr_index=70,
        address=0x1000,
    )
    mlil = types.SimpleNamespace(instructions=[jump])
    plan = {
        "condition_var": "idx",
        "condition_size": 8,
        "condition_value": 1,
        "true": 10,
        "false": 20,
        "cleanup_roots": {31},
    }
    built = []

    class NewMLIL:
        def get_label_for_source_instruction(self, instr_index):
            return types.SimpleNamespace(operand=("label", instr_index))

        def var(self, size, var_):
            return ("var", size, var_)

        def const(self, size, value):
            return ("const", size, value)

        def compare_equal(self, size, left, right):
            return ("cmp_e", size, left, right)

        def if_expr(self, condition, true_label, false_label, loc):
            built.append((condition, true_label.operand, false_label.operand, loc))
            return "if-expr"

    def fake_plan(_bv, _mlil, ins, _address_escapes, _variables_are_local):
        assert ins is jump
        return plan

    def fake_copy(_ctx, replacements, mlil=None):
        assert mlil is not None
        assert set(replacements) == {7}
        replacements[7](NewMLIL(), jump)
        return "new-mlil", 1

    monkeypatch.setattr(branch_conditions, "_plan_for_jump", fake_plan)
    monkeypatch.setattr(branch_conditions, "copy_mlil_with_instruction_rewrites", fake_copy)

    new_mlil, applied, cleanup_roots = branch_conditions.translate_indirect_branch_conditions(
        None,
        types.SimpleNamespace(mlil=mlil, llil=object()),
        mlil,
    )

    assert new_mlil == "new-mlil"
    assert applied == 1
    assert cleanup_roots == {31}
    assert built == [
        (
            ("cmp_e", 0, ("var", 8, "idx"), ("const", 8, 1)),
            ("label", 10),
            ("label", 20),
            ("loc", 70),
        )
    ]


def test_translate_branch_conditions_caches_address_escape_per_current_variable(monkeypatch):
    class Variable:
        def __init__(self, identity):
            self.identity = identity

        def __eq__(self, other):
            return isinstance(other, Variable) and self.identity == other.identity

        def __hash__(self):
            return hash(self.identity)

        def __str__(self):
            return "same display name"

    instructions = [
        types.SimpleNamespace(variable=Variable(1)),
        types.SimpleNamespace(variable=Variable(1)),
        types.SimpleNamespace(variable=Variable(2)),
        types.SimpleNamespace(variable=Variable(2)),
    ]
    mlil = types.SimpleNamespace(instructions=instructions)
    scans = []
    checkers = []

    def build_checker(_mlil):
        results = {}

        def scan(variable):
            if variable not in results:
                scans.append(variable.identity)
                results[variable] = False
            return results[variable]

        checkers.append(scan)
        return scan

    def plan(
        _bv,
        _mlil,
        instruction,
        address_escapes,
        _variables_are_local,
    ):
        address_escapes(instruction.variable)
        return None

    monkeypatch.setattr(branch_conditions, "address_escape_checker", build_checker)
    monkeypatch.setattr(branch_conditions, "_plan_for_jump", plan)

    assert branch_conditions.translate_indirect_branch_conditions(None, None, mlil) == (
        mlil,
        0,
        set(),
    )
    assert scans == [1, 2]
    assert len(checkers) == 1

    branch_conditions.translate_indirect_branch_conditions(None, None, mlil)

    assert scans == [1, 2, 1, 2]
    assert len(checkers) == 2


def test_translate_owned_diamond_rewrites_the_existing_source_if(monkeypatch):
    condition = var("program_condition")
    source_if = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_IF,
        instr_index=3,
        expr_index=30,
        address=0x900,
        condition=condition,
    )
    jump = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        instr_index=7,
        expr_index=70,
        address=0x1000,
    )
    mlil = types.SimpleNamespace(instructions=[jump])
    plan = {
        "rewrite_il": source_if,
        "condition_from_rewrite": True,
        "true": 10,
        "false": 20,
        "cleanup_roots": {31},
    }
    built = []

    class NewMLIL:
        def get_label_for_source_instruction(self, instr_index):
            return types.SimpleNamespace(operand=("label", instr_index))

        def copy_expr(self, expression):
            return ("copied", expression)

        def if_expr(self, copied_condition, true_label, false_label, loc):
            built.append((copied_condition, true_label.operand, false_label.operand, loc))
            return "if-expr"

    def fake_copy(_ctx, replacements, mlil=None):
        assert mlil is not None
        assert set(replacements) == {3}
        replacements[3](NewMLIL(), source_if)
        return "new-mlil", 1

    monkeypatch.setattr(branch_conditions, "_plan_for_jump", lambda *_args: plan)
    monkeypatch.setattr(branch_conditions, "copy_mlil_with_instruction_rewrites", fake_copy)

    new_mlil, applied, cleanup_roots = branch_conditions.translate_indirect_branch_conditions(
        None,
        types.SimpleNamespace(mlil=mlil, llil=object()),
        mlil,
    )

    assert new_mlil == "new-mlil"
    assert applied == 1
    assert cleanup_roots == {31}
    assert built == [
        (("copied", condition), ("label", 10), ("label", 20), ("loc", 30))
    ]


def test_translate_rejects_two_plans_claiming_one_source_if(monkeypatch):
    source_if = types.SimpleNamespace(instr_index=3)
    jumps = [
        types.SimpleNamespace(
            operation=MediumLevelILOperation.MLIL_JUMP_TO,
            instr_index=index,
            address=0x1000 + index,
        )
        for index in (7, 8)
    ]
    mlil = types.SimpleNamespace(instructions=jumps)
    plan = {
        "rewrite_il": source_if,
        "condition_from_rewrite": True,
        "true": 10,
        "false": 20,
        "cleanup_roots": set(),
    }
    copy_calls = []
    monkeypatch.setattr(branch_conditions, "_plan_for_jump", lambda *_args: plan)
    monkeypatch.setattr(
        branch_conditions,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: copy_calls.append(True),
    )

    new_mlil, applied, cleanup_roots = branch_conditions.translate_indirect_branch_conditions(
        None,
        types.SimpleNamespace(mlil=mlil, llil=object()),
        mlil,
    )

    assert new_mlil is None
    assert applied == 0
    assert cleanup_roots == set()
    assert copy_calls == []


def test_translate_branch_conditions_does_not_cleanup_when_copy_rewrite_fails(monkeypatch):
    jump = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        instr_index=7,
        expr_index=70,
        address=0x1000,
    )
    mlil = types.SimpleNamespace(instructions=[jump])
    plan = {
        "condition_var": "idx",
        "condition_size": 8,
        "condition_value": 1,
        "true": 10,
        "false": 20,
        "cleanup_roots": {31},
    }

    monkeypatch.setattr(
        branch_conditions,
        "_plan_for_jump",
        lambda _bv, _mlil, _ins, _address_escapes, _variables_are_local: plan,
    )
    monkeypatch.setattr(
        branch_conditions,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("copy failed")),
    )

    new_mlil, applied, cleanup_roots = branch_conditions.translate_indirect_branch_conditions(
        None,
        types.SimpleNamespace(mlil=mlil, llil=object()),
        mlil,
    )

    assert new_mlil is None
    assert applied == 0
    assert cleanup_roots == set()


def test_translate_branch_conditions_rejects_plan_without_instruction_index(monkeypatch):
    indexed = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        instr_index=7,
        address=0x1000,
    )
    unindexed = types.SimpleNamespace(
        operation=MediumLevelILOperation.MLIL_JUMP_TO,
        address=0x2000,
    )
    mlil = types.SimpleNamespace(instructions=[indexed, unindexed])
    plans = {
        id(indexed): {
            "condition_var": "first",
            "condition_size": 8,
            "condition_value": 1,
            "true": 10,
            "false": 20,
            "cleanup_roots": {31},
        },
        id(unindexed): {
            "condition_var": "second",
            "condition_size": 8,
            "condition_value": 1,
            "true": 30,
            "false": 40,
            "cleanup_roots": {32},
        },
    }
    copy_calls = []

    monkeypatch.setattr(
        branch_conditions,
        "_plan_for_jump",
        lambda _bv, _mlil, ins, _address_escapes, _variables_are_local: plans[id(ins)],
    )
    monkeypatch.setattr(
        branch_conditions,
        "copy_mlil_with_instruction_rewrites",
        lambda *_args, **_kwargs: (copy_calls.append(True), ("partial", 1))[1],
    )

    new_mlil, applied, cleanup_roots = branch_conditions.translate_indirect_branch_conditions(
        None,
        types.SimpleNamespace(mlil=mlil, llil=object()),
        mlil,
    )

    assert new_mlil is None
    assert applied == 0
    assert cleanup_roots == set()
    assert copy_calls == []


def test_copy_rewrite_requires_copied_target_labels():
    mlil = types.SimpleNamespace(get_label_for_source_instruction=lambda _idx: None)

    try:
        branch_conditions.copied_label_for_source(mlil, 10)
    except ValueError as e:
        assert "source instruction 10" in str(e)
    else:
        raise AssertionError("missing copied label should fail the rewrite")


if __name__ == "__main__":
    test_bool_to_int_jump_maps_true_and_false_targets()
    test_jump_target_eval_handles_negated_multiply()
    test_eval_const_accepts_multiple_defs_when_they_agree()
    test_assigned_target_var_plan_does_not_require_same_source_if()
    test_translate_branch_conditions_builds_copy_rewrite()
    test_translate_branch_conditions_does_not_cleanup_when_copy_rewrite_fails()
    test_translate_branch_conditions_rejects_plan_without_instruction_index()
    test_copy_rewrite_requires_copied_target_labels()
