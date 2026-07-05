from conftest import load_plugin_module


nop_pass = load_plugin_module("plugins.DispatchThis.passes.medium.nop_pass")


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    _next_index = 1

    def __init__(self, op, **attrs):
        self.operation = Op(op)
        self.expr_index = attrs.pop("expr_index", Expr._next_index)
        self.instr_index = attrs.pop("instr_index", self.expr_index)
        Expr._next_index += 1
        self.address = attrs.pop("address", 0x1000 + self.expr_index)
        self.__dict__.update(attrs)

    def traverse(self, visit):
        yield visit(self)
        for value in self.__dict__.values():
            if isinstance(value, Expr):
                yield from value.traverse(visit)


class FakeMlil:
    def __init__(self, instructions):
        self.instructions = list(instructions)
        self.replacements = []

    def replace_expr(self, expr_index, expr):
        self.replacements.append((expr_index, expr))

    def nop(self, loc):
        return ("nop", loc)

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        self.ssa_generated = True


def const(value):
    return Expr("MLIL_CONST", constant=value)


def set_var(dest, value):
    return Expr("MLIL_SET_VAR", dest=dest, src=const(value))


def test_ref_consts_reports_full_and_legacy_low32_values():
    ins = set_var("tmp", 0x6C5B6887819676A8)

    refs = nop_pass._ref_consts(ins)

    assert 0x6C5B6887819676A8 in refs
    assert 0x819676A8 in refs


def test_nop_state_writes_matches_full_width_state_constants():
    ins = set_var("tmp", 0x6C5B6887819676A8)
    mlil = FakeMlil([ins])

    count = nop_pass._nop_state_writes(mlil, {0x6C5B6887819676A8}, set())

    assert count == 1
    assert mlil.replacements == [(ins.expr_index, ("nop", ("loc", ins.expr_index)))]
    assert mlil.finalized is True
    assert mlil.ssa_generated is True


def test_nop_state_writes_keeps_legacy_low32_state_constant_match():
    ins = set_var("tmp", 0x6C5B6887819676A8)
    mlil = FakeMlil([ins])

    count = nop_pass._nop_state_writes(mlil, {0x819676A8}, set())

    assert count == 1


if __name__ == "__main__":
    test_ref_consts_reports_full_and_legacy_low32_values()
    test_nop_state_writes_matches_full_width_state_constants()
    test_nop_state_writes_keeps_legacy_low32_state_constant_match()
