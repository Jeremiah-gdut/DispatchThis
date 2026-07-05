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
    def get_var_definitions(self, _var):
        return []


def const(value):
    return Expr("MLIL_CONST", constant=value)


def cmp_ne(left, right):
    return Expr("MLIL_CMP_NE", [left, right], left=left, right=right)


def bool_to_int(cond):
    return Expr("MLIL_BOOL_TO_INT", [cond], src=cond)


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


if __name__ == "__main__":
    test_bool_to_int_jump_maps_true_and_false_targets()
