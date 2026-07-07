import types

from conftest import load_plugin_module


indirect_calls = load_plugin_module("plugins.DispatchThis.passes.medium.indirect_calls")


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
        self.__dict__.update(attrs)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out


class FakeBv:
    def __init__(self):
        self.arch = types.SimpleNamespace(address_size=8)
        self.memory = {}
        self.valid_offsets = set()
        self.symbols = {}
        self.functions = {}

    def read(self, addr, size):
        return self.memory.get(addr, b"")[:size]

    def is_valid_offset(self, addr):
        return addr in self.valid_offsets

    def get_symbol_at(self, addr):
        return self.symbols.get(addr)

    def get_function_at(self, addr):
        return self.functions.get(addr)


class FakeMlil:
    def __init__(self, instructions, defs=None):
        self.instructions = list(instructions)
        self._defs = defs or {}
        self.replaced = []
        self.source_function = types.SimpleNamespace(name="sub_4000")
        self.finalized = False

    def get_var_definitions(self, var):
        return self._defs.get(var, [])

    def replace_expr(self, expr_index, replacement):
        self.replaced.append((expr_index, replacement))

    @staticmethod
    def const_pointer(size, target):
        return ("const_ptr", size, target)

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        pass


def const(value):
    return Expr("MLIL_CONST_PTR", constant=value)


def var(name):
    return Expr("MLIL_VAR", src=name)


def add(left, right):
    return Expr("MLIL_ADD", [left, right], left=left, right=right)


def load(src, size=8):
    return Expr("MLIL_LOAD", [src], src=src, size=size)


def set_var(dest, src, instr_index, address=0x4010):
    return Expr("MLIL_SET_VAR", [src], dest=dest, src=src, instr_index=instr_index, address=address)


def call(dest, address=0x4000):
    return Expr("MLIL_CALL", [dest], dest=dest, address=address)


def decoded_call_fixture():
    bv = FakeBv()
    bv.memory[0x1000] = (0x4FF0).to_bytes(8, "little")
    bv.valid_offsets.add(0x5000)
    bv.functions[0x5000] = object()
    bv.symbols[0x5000] = types.SimpleNamespace(name="target")

    decode = add(load(const(0x1000)), const(0x10))
    decode_def = set_var("target", decode, instr_index=7)
    call_il = call(var("target"))
    mlil = FakeMlil([decode_def, call_il], {"target": [decode_def]})
    return bv, mlil, call_il, decode_def


def test_indirect_call_plan_preserves_call_fact_output():
    bv, mlil, call_il, decode_def = decoded_call_fixture()

    assert indirect_calls.plan_indirect_calls(bv, mlil) == [{
        "call_il": call_il,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": decode_def,
        "cleanup_roots": {7},
    }]


def test_indirect_call_rewrite_replaces_call_dest_and_decode_source():
    bv, mlil, _call_il, _decode_def = decoded_call_fixture()
    plans = indirect_calls.plan_indirect_calls(bv, mlil)

    assert indirect_calls.apply_indirect_call_rewrites(bv, mlil, plans) == 1
    assert mlil.replaced == [
        (plans[0]["call_il"].dest.expr_index, ("const_ptr", 8, 0x5000)),
        (plans[0]["decode_def"].src.expr_index, ("const_ptr", 8, 0x5000)),
    ]
    assert mlil.finalized is True


if __name__ == "__main__":
    test_indirect_call_plan_preserves_call_fact_output()
    test_indirect_call_rewrite_replaces_call_dest_and_decode_source()
