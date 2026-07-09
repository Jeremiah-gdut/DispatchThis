import types

from conftest import load_plugin_module


branch_conditions = load_plugin_module("plugins.DispatchThis.passes.medium.branch_conditions")


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


def test_bool_to_int_jump_maps_true_and_false_targets():
    cond = cmp_ne(const(1), const(0))
    dest = add(const(0x1000), lsl(bool_to_int(cond), const(6)))
    jump = types.SimpleNamespace(
        operation=Op("MLIL_JUMP_TO"),
        targets={0x1000: 10, 0x1040: 20},
        dest=dest,
    )

    plan = branch_conditions._plan_for_jump(None, FakeMLIL(), jump)

    assert plan["condition"] is cond
    assert plan["true"] == 20
    assert plan["false"] == 10


def test_jump_target_eval_handles_negated_multiply():
    idx = "idx"
    dest = add(const(0x1040), mul(var(idx), neg(const(0x20))))
    jump = types.SimpleNamespace(
        targets={0x1040: 10, 0x1000: 20},
        dest=dest,
    )

    assert branch_conditions._target_for_value(None, FakeMLIL(), jump, idx, 0) == 10
    assert branch_conditions._target_for_value(None, FakeMLIL(), jump, idx, 2) == 20


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


if __name__ == "__main__":
    test_bool_to_int_jump_maps_true_and_false_targets()
    test_jump_target_eval_handles_negated_multiply()
    test_eval_const_accepts_multiple_defs_when_they_agree()
    test_assigned_target_var_plan_does_not_require_same_source_if()
