import types
from collections import namedtuple

from binaryninja import MediumLevelILOperation

from conftest import load_plugin_module


indirect_calls = load_plugin_module("plugins.DispatchThis.passes.medium.deincall")
SsaVar = namedtuple("SsaVar", ("var", "version"))


class Expr:
    _next_index = 1

    def __init__(self, op, children=(), **attrs):
        self.operation = MediumLevelILOperation[op]
        self.children = list(children)
        self.expr_index = Expr._next_index
        Expr._next_index += 1
        self.__dict__.update(attrs)

    def traverse(self, visit):
        out = [visit(self)]
        for child in self.children:
            out.extend(child.traverse(visit))
        return out

    def copy_to(self, dest, sub_expr_handler=None):
        children = (
            tuple(sub_expr_handler(child) for child in self.children)
            if sub_expr_handler is not None
            else ()
        )
        return ("copy", self.expr_index, children)


class SsaUnavailableCall(Expr):
    @property
    def ssa_form(self):
        raise AssertionError("SSA form is unavailable")


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
        for instruction in self.instructions:
            instruction.function = self
        self._defs = defs or {}
        self.replaced = []
        self.source_function = types.SimpleNamespace(name="sub_4000")
        self.finalized = False
        self.ssa_form = None

    def get_var_definitions(self, var):
        return self._defs.get(var, [])

    def __getitem__(self, index):
        return self.instructions[index]

    def replace_expr(self, expr_index, replacement):
        self.replaced.append((expr_index, replacement))

    @staticmethod
    def const_pointer(size, target):
        return ("const_ptr", size, target)

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        pass


class FakeSsa:
    def __init__(self, definitions):
        self._definitions = dict(definitions)

    def get_ssa_var_definition(self, variable):
        return self._definitions.get(variable)


def const(value):
    return Expr("MLIL_CONST_PTR", constant=value)


def var(name):
    return Expr("MLIL_VAR", src=name)


def var_ssa(variable):
    return Expr("MLIL_VAR_SSA", src=variable)


def add(left, right):
    return Expr("MLIL_ADD", [left, right], left=left, right=right)


def load(src, size=8):
    return Expr("MLIL_LOAD", [src], src=src, size=size)


def load_struct(src, offset, size=8):
    return Expr(
        "MLIL_LOAD_STRUCT",
        [src],
        src=src,
        offset=offset,
        size=size,
    )


def set_var(dest, src, instr_index, address=0x4010):
    return Expr("MLIL_SET_VAR", [src], dest=dest, src=src, instr_index=instr_index, address=address)


def call(dest, params=(), address=0x4000, instr_index=0):
    return Expr(
        "MLIL_CALL",
        [dest, *params],
        dest=dest,
        params=list(params),
        address=address,
        instr_index=instr_index,
    )


def set_var_ssa(dest, src, non_ssa):
    return Expr(
        "MLIL_SET_VAR_SSA",
        [src],
        dest=dest,
        src=src,
        instr_index=non_ssa.instr_index,
        address=non_ssa.address,
        non_ssa_form=non_ssa,
    )


def var_phi(dest, sources):
    return Expr("MLIL_VAR_PHI", dest=dest, src=list(sources))


def attach_call_ssa(mlil, call_il, dest, definitions):
    ssa = FakeSsa(definitions)
    ssa_call = Expr(
        "MLIL_CALL_SSA",
        [dest],
        dest=dest,
        params=[],
        address=call_il.address,
        instr_index=call_il.instr_index,
        non_ssa_form=call_il,
    )
    ssa_call.function = ssa
    call_il.ssa_form = ssa_call
    mlil.ssa_form = ssa


def decoded_call_fixture():
    bv = FakeBv()
    bv.memory[0x1000] = (0x4FF0).to_bytes(8, "little")
    bv.valid_offsets.add(0x5000)
    bv.functions[0x5000] = object()
    bv.symbols[0x5000] = types.SimpleNamespace(name="target")

    decode = add(load(const(0x1000)), const(0x10))
    decode_def = set_var("target", decode, instr_index=0)
    call_il = call(var("target"), instr_index=1)
    mlil = FakeMlil([decode_def, call_il], {"target": [decode_def]})
    target = SsaVar("target", 1)
    target_def = set_var_ssa(target, decode, decode_def)
    attach_call_ssa(mlil, call_il, var_ssa(target), {target: target_def})
    return bv, mlil, call_il, decode_def


def test_indirect_call_plan_preserves_call_fact_output():
    bv, mlil, call_il, decode_def = decoded_call_fixture()

    assert indirect_calls.plan_indirect_calls(bv, mlil) == [{
        "call_il": call_il,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": decode_def,
    }]


def test_unresolved_call_target_keeps_the_two_part_result_contract(monkeypatch):
    bv, mlil, call_il, _decode_def = decoded_call_fixture()
    monkeypatch.setattr(
        indirect_calls,
        "fold_constant_value",
        lambda *_args, **_kwargs: None,
    )

    assert indirect_calls.resolve_call_target(bv, mlil, call_il) == (None, None)
    assert indirect_calls.plan_indirect_calls(bv, mlil) == []


def test_call_plan_marks_only_loads_in_the_full_target_definition_slice():
    bv = FakeBv()
    bv.memory[0x1000] = (0x4FF0).to_bytes(8, "little")
    bv.memory[0x2000] = (0x1234).to_bytes(8, "little")
    bv.valid_offsets.add(0x5000)
    bv.functions[0x5000] = object()

    encoded = set_var("encoded", load(const(0x1000)), instr_index=0)
    decoded = set_var(
        "target",
        add(var("encoded"), const(0x10)),
        instr_index=1,
    )
    unrelated = set_var("unrelated", load(const(0x2000)), instr_index=2)
    call_il = call(var("target"), instr_index=3)
    mlil = FakeMlil(
        [encoded, decoded, unrelated, call_il],
        {
            "encoded": [encoded],
            "target": [decoded],
            "unrelated": [unrelated],
        },
    )
    encoded_ssa = SsaVar("encoded", 1)
    target_ssa = SsaVar("target", 1)
    encoded_def = set_var_ssa(encoded_ssa, encoded.src, encoded)
    target_def = set_var_ssa(
        target_ssa,
        add(var_ssa(encoded_ssa), const(0x10)),
        decoded,
    )
    attach_call_ssa(
        mlil,
        call_il,
        var_ssa(target_ssa),
        {encoded_ssa: encoded_def, target_ssa: target_def},
    )

    plan = indirect_calls.plan_indirect_calls(bv, mlil)[0]

    assert "cleanup_roots" not in plan
    assert "cleanup_load_roots" not in plan

    stale = {**plan, "cleanup_roots": {1, 999}, "cleanup_load_roots": {1, 999}}
    rebound = indirect_calls.validate_current_call_plans(mlil, [stale])

    assert rebound[0]["cleanup_roots"] == {0, 1}
    assert rebound[0]["cleanup_load_roots"] == {0}
    assert rebound[0]["cleanup_proven"] is True


def test_call_cleanup_slice_uses_only_the_ssa_definition_reaching_the_call():
    bv = FakeBv()
    for slot in (0x1000, 0x2000):
        bv.memory[slot] = (0x4FF0).to_bytes(8, "little")
    bv.valid_offsets.add(0x5000)
    bv.functions[0x5000] = object()

    before = set_var("encoded", load(const(0x1000)), instr_index=0)
    decoded = set_var(
        "target",
        add(var("encoded"), const(0x10)),
        instr_index=1,
    )
    call_il = call(var("target"), instr_index=2)
    after = set_var("encoded", load(const(0x2000)), instr_index=3)
    mlil = FakeMlil(
        [before, decoded, call_il, after],
        {"encoded": [before, after], "target": [decoded]},
    )

    encoded_before = SsaVar("encoded", 1)
    target = SsaVar("target", 1)
    encoded_after = SsaVar("encoded", 2)
    attach_call_ssa(
        mlil,
        call_il,
        var_ssa(target),
        {
            encoded_before: set_var_ssa(encoded_before, before.src, before),
            target: set_var_ssa(
                target,
                add(var_ssa(encoded_before), const(0x10)),
                decoded,
            ),
            encoded_after: set_var_ssa(encoded_after, after.src, after),
        },
    )

    plan = indirect_calls.validate_current_call_plans(
        mlil,
        indirect_calls.plan_indirect_calls(bv, mlil),
    )[0]

    assert plan["cleanup_roots"] == {0, 1}
    assert plan["cleanup_load_roots"] == {0}
    assert 3 not in plan["cleanup_roots"]


def test_call_cleanup_slice_follows_every_exact_phi_input():
    bv = FakeBv()
    for slot in (0x1000, 0x2000):
        bv.memory[slot] = (0x4FF0).to_bytes(8, "little")
    bv.valid_offsets.add(0x5000)
    bv.functions[0x5000] = object()

    left = set_var("encoded", load(const(0x1000)), instr_index=0)
    right = set_var("encoded", load(const(0x2000)), instr_index=1)
    decoded = set_var(
        "target",
        add(var("encoded"), const(0x10)),
        instr_index=2,
    )
    call_il = call(var("target"), instr_index=3)
    mlil = FakeMlil(
        [left, right, decoded, call_il],
        {"encoded": [left, right], "target": [decoded]},
    )

    encoded_left = SsaVar("encoded", 1)
    encoded_right = SsaVar("encoded", 2)
    encoded_join = SsaVar("encoded", 3)
    target = SsaVar("target", 1)
    attach_call_ssa(
        mlil,
        call_il,
        var_ssa(target),
        {
            encoded_left: set_var_ssa(encoded_left, left.src, left),
            encoded_right: set_var_ssa(encoded_right, right.src, right),
            encoded_join: var_phi(encoded_join, (encoded_left, encoded_right)),
            target: set_var_ssa(
                target,
                add(var_ssa(encoded_join), const(0x10)),
                decoded,
            ),
        },
    )

    plan = indirect_calls.validate_current_call_plans(
        mlil,
        indirect_calls.plan_indirect_calls(bv, mlil),
    )[0]

    assert plan["cleanup_roots"] == {0, 1, 2}
    assert plan["cleanup_load_roots"] == {0, 1}


def test_call_cleanup_slice_has_no_fixed_definition_depth_limit():
    definitions = {}
    instructions = []
    previous = SsaVar("value0", 0)
    for index in range(1, 67):
        current = SsaVar(f"value{index}", 1)
        assignment = set_var(f"value{index}", var(f"value{index - 1}"), instr_index=index - 1)
        instructions.append(assignment)
        definitions[current] = set_var_ssa(current, var_ssa(previous), assignment)
        previous = current
    call_il = call(var("value66"), instr_index=66)
    mlil = FakeMlil([*instructions, call_il])
    attach_call_ssa(mlil, call_il, var_ssa(previous), definitions)

    plans = indirect_calls.validate_current_call_plans(
        mlil,
        [{"call_il": call_il, "call_addr": 0x4000, "target": 0x5000}],
    )

    assert plans[0]["cleanup_proven"] is True
    assert plans[0]["cleanup_roots"] == set(range(66))


def test_unprovable_ssa_slice_disables_cleanup_without_losing_resolution():
    bv, mlil, call_il, _decode_def = decoded_call_fixture()
    aliased_target = Expr(
        "MLIL_VAR_SSA_FIELD",
        src=SsaVar("target", 1),
        offset=0,
    )
    call_il.ssa_form.dest = aliased_target
    call_il.ssa_form.children = [aliased_target]

    plan = indirect_calls.validate_current_call_plans(
        mlil,
        indirect_calls.plan_indirect_calls(bv, mlil),
    )[0]

    assert plan["target"] == 0x5000
    assert plan["cleanup_proven"] is False
    assert plan["cleanup_roots"] == set()
    assert "cleanup_load_roots" not in plan


def test_missing_function_ssa_does_not_block_call_target_resolution():
    bv, _old_mlil, _old_call, _old_decode = decoded_call_fixture()
    decode = add(load(const(0x1000)), const(0x10))
    decode_def = set_var("target", decode, instr_index=0)
    dest = var("target")
    call_il = SsaUnavailableCall(
        "MLIL_CALL",
        [dest],
        dest=dest,
        params=[],
        address=0x4000,
        instr_index=1,
    )
    mlil = FakeMlil([decode_def, call_il], {"target": [decode_def]})

    plan = indirect_calls.validate_current_call_plans(
        mlil,
        indirect_calls.plan_indirect_calls(bv, mlil),
    )[0]

    assert plan["target"] == 0x5000
    assert plan["cleanup_proven"] is False
    assert plan["cleanup_roots"] == set()
    assert "cleanup_load_roots" not in plan


def test_indirect_call_rewrite_mutates_only_current_call_destinations():
    bv, mlil, call_il, _decode_def = decoded_call_fixture()
    plans = indirect_calls.plan_indirect_calls(bv, mlil)

    rewritten, applied = indirect_calls.apply_indirect_call_rewrites(
        types.SimpleNamespace(view=bv),
        mlil,
        plans,
    )

    assert rewritten is mlil
    assert applied == 1
    assert mlil.replaced == [(call_il.dest.expr_index, ("const_ptr", 8, 0x5000))]
    assert mlil.finalized is True


def test_profile_decode_witness_is_never_a_rewrite_target():
    bv, mlil, _call_il, _decode_def = decoded_call_fixture()
    plan = indirect_calls.plan_indirect_calls(bv, mlil)[0]
    unrelated = set_var("ordinary", add(const(1), const(2)), instr_index=2)
    unrelated.function = mlil
    mlil.instructions.append(unrelated)
    plan["decode_def"] = unrelated
    call_dest_index = plan["call_il"].dest.expr_index

    _new_mlil, applied = indirect_calls.apply_indirect_call_rewrites(
        types.SimpleNamespace(view=bv),
        mlil,
        [plan],
    )

    assert applied == 1
    assert mlil.replaced == [(call_dest_index, ("const_ptr", 8, 0x5000))]
    assert unrelated.expr_index not in {index for index, _replacement in mlil.replaced}


def test_current_call_plan_rejects_a_witness_from_regenerated_mlil():
    bv, mlil, call_il, decode_def = decoded_call_fixture()
    plan = indirect_calls.plan_indirect_calls(bv, mlil)[0]
    call_il.function = object()

    assert indirect_calls.validate_current_call_plans(mlil, [plan]) is None
    assert indirect_calls.apply_indirect_call_rewrites(
        types.SimpleNamespace(view=bv), mlil, [plan]
    ) == (mlil, 0)
    assert mlil.replaced == []


def test_current_call_facts_require_an_exact_indirect_call_witness():
    _bv, mlil, call_il, _decode_def = decoded_call_fixture()

    assert indirect_calls.validate_current_call_facts(
        mlil,
        [(call_il, (0x5000, 0x6000))],
    ) == [(call_il, (0x5000, 0x6000))]

    call_il.function = object()

    assert indirect_calls.validate_current_call_facts(
        mlil,
        [(call_il, (0x5000, 0x6000))],
    ) is None


def test_current_call_plan_rejects_mismatched_call_address():
    bv, mlil, _call_il, _decode_def = decoded_call_fixture()
    plan = indirect_calls.plan_indirect_calls(bv, mlil)[0]
    plan["call_addr"] += 4

    assert indirect_calls.validate_current_call_plans(mlil, [plan]) is None


def test_current_call_plan_rejects_regenerated_parameters():
    bv, mlil, _call_il, _decode_def = decoded_call_fixture()
    plan = indirect_calls.plan_indirect_calls(bv, mlil)[0]
    replacement = call(plan["call_il"].dest, [const(7)], instr_index=1)
    replacement.expr_index = plan["call_il"].expr_index
    replacement.function = mlil
    mlil.instructions[1] = replacement

    assert indirect_calls.validate_current_call_plans(mlil, [plan]) is None


def test_current_call_plan_collapses_exact_duplicates_and_rejects_target_conflicts():
    bv, mlil, _call_il, _decode_def = decoded_call_fixture()
    plan = indirect_calls.plan_indirect_calls(bv, mlil)[0]

    rebound = indirect_calls.validate_current_call_plans(mlil, [plan, dict(plan)])
    assert len(rebound) == 1
    assert rebound[0]["call_il"] is plan["call_il"]
    assert rebound[0]["cleanup_roots"] == {0}
    assert indirect_calls.validate_current_call_plans(
        mlil,
        [plan, {**plan, "target": 0x6000}],
    ) is None


def test_call_target_receipt_rebinds_only_an_exact_current_direct_call():
    direct = call(const(0x5000), instr_index=0)
    mlil = FakeMlil([direct])

    assert indirect_calls.current_call_receipt_plans(mlil, {0x4000: 0x5000}) == [{
        "call_il": direct,
        "call_addr": 0x4000,
        "target": 0x5000,
        "decode_def": None,
    }]
    assert indirect_calls.current_call_receipt_plans(mlil, {0x4000: 0x6000}) is None


def test_indirect_call_plan_rejects_divergent_reaching_definitions():
    bv = FakeBv()
    bv.memory[0x1000] = (0x4FF0).to_bytes(8, "little")
    bv.memory[0x2000] = (0x5FF0).to_bytes(8, "little")
    for target in (0x5000, 0x6000):
        bv.valid_offsets.add(target)
        bv.functions[target] = object()

    first = set_var("target", add(load(const(0x1000)), const(0x10)), instr_index=7)
    second = set_var("target", add(load(const(0x2000)), const(0x10)), instr_index=8)
    call_il = call(var("target"))
    mlil = FakeMlil([first, second, call_il], {"target": [first, second]})

    assert indirect_calls.plan_indirect_calls(bv, mlil) == []


def test_indirect_call_outer_load_resolves_loaded_pointer_not_slot_address():
    bv = FakeBv()
    bv.memory[0x5000] = (0x6000).to_bytes(8, "little")
    for target in (0x5000, 0x6000):
        bv.valid_offsets.add(target)
        bv.functions[target] = object()
    call_il = call(load(add(const(0x4FF0), const(0x10))))
    mlil = FakeMlil([call_il])

    plans = indirect_calls.plan_indirect_calls(bv, mlil)

    assert len(plans) == 1
    assert plans[0]["target"] == 0x6000
    assert plans[0]["decode_def"] is None


def test_indirect_call_outer_struct_load_includes_field_offset():
    bv = FakeBv()
    bv.memory[0x5020] = (0x6000).to_bytes(8, "little")
    bv.valid_offsets.add(0x6000)
    bv.functions[0x6000] = object()
    call_il = call(load_struct(add(const(0x4FF0), const(0x10)), 0x20))

    plans = indirect_calls.plan_indirect_calls(bv, FakeMlil([call_il]))

    assert len(plans) == 1
    assert plans[0]["target"] == 0x6000


def test_indirect_call_outer_load_rejects_a_known_low48_alias():
    bv = FakeBv()
    bv.memory[0x5000] = (0xFFFF000000006000).to_bytes(8, "little")
    bv.valid_offsets.add(0x6000)
    bv.functions[0x6000] = object()
    call_il = call(load(add(const(0x4FF0), const(0x10))))

    assert indirect_calls.plan_indirect_calls(bv, FakeMlil([call_il])) == []


def test_indirect_call_u48_decode_does_not_select_the_unwrapped_sum():
    bv = FakeBv()
    bv.valid_offsets.add(0x1000000000000)
    bv.functions[0x1000000000000] = object()
    call_il = call(add(const(0xFFFFFFFFFFFF), const(1)))

    assert indirect_calls.plan_indirect_calls(bv, FakeMlil([call_il])) == []


if __name__ == "__main__":
    test_indirect_call_plan_preserves_call_fact_output()
