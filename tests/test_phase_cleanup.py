from binaryninja import MediumLevelILOperation as M, VariableSourceType

from conftest import load_plugin_module


phase_cleanup = load_plugin_module("plugins.DispatchThis.passes.medium.phase_cleanup")

_candidate_slice = phase_cleanup._candidate_slice
_drop_live_escapes = phase_cleanup._drop_live_escapes


class Expr:
    def __init__(self, operation, children=()):
        self.operation = operation
        self.children = tuple(children)

    def traverse(self, visit):
        yield visit(self)
        for child in self.children:
            yield from child.traverse(visit)


class Var:
    def __init__(self, name, version, source_type=None):
        self.name = name
        self.version = version
        self.source_type = source_type


class Ins:
    def __init__(self, idx, op, reads=(), writes=(), non_ssa=None, src=None):
        self.instr_index = idx
        self.operation = M[op]
        self.vars_read = list(reads)
        self.vars_written = list(writes)
        self.non_ssa_form = non_ssa
        self.src = src if src is not None else Expr(M.MLIL_CONST)


class NonSSA:
    def __init__(self, idx, expr_index=None, src=None):
        self.instr_index = idx
        self.expr_index = expr_index if expr_index is not None else idx
        self.operation = M.MLIL_SET_VAR
        self.src = src if src is not None else Expr(M.MLIL_CONST)


class FakeSSA:
    def __init__(self, uses=None, instructions=None):
        self.instructions = list(instructions or [])
        self.uses = uses or {}

    def get_ssa_var_uses(self, var):
        return self.uses.get(var, [])


class FakeMLIL:
    def __init__(self, ssa, instruction_count=None):
        self.ssa_form = ssa
        self.instruction_count = instruction_count
        self.replaced = []
        self.finalized = False

    def __len__(self):
        if self.instruction_count is None:
            return len(self.ssa_form.instructions)
        return self.instruction_count

    def replace_expr(self, expr_index, replacement):
        self.replaced.append((expr_index, replacement))

    @staticmethod
    def nop(location):
        return ("nop", location)

    def finalize(self):
        self.finalized = True

    def generate_ssa_form(self):
        pass


def test_phi_only_use_does_not_keep_decode_candidate_live():
    a = Var("a", 1)
    b = Var("b", 1)
    c = Var("c", 1)

    candidate = Ins(1, "MLIL_SET_VAR_SSA", writes=[a])
    phi1 = Ins(2, "MLIL_VAR_PHI", reads=[a], writes=[b])
    phi2 = Ins(3, "MLIL_VAR_PHI", reads=[b], writes=[c])

    kept = _drop_live_escapes(FakeSSA({a: [phi1], b: [phi2]}), {1}, {1: candidate})

    assert kept == {1}


def test_phi_chain_with_real_use_keeps_decode_candidate_live():
    a = Var("a", 1)
    b = Var("b", 1)

    candidate = Ins(1, "MLIL_SET_VAR_SSA", writes=[a])
    phi = Ins(2, "MLIL_VAR_PHI", reads=[a], writes=[b])
    real_use = Ins(3, "MLIL_IF", reads=[b])

    kept = _drop_live_escapes(FakeSSA({a: [phi], b: [real_use]}), {1}, {1: candidate})

    assert kept == set()


def test_callback_pointer_used_as_call_parameter_is_never_cleaned():
    callback = Var("callback", 1)
    callback_definition = Ins(1, "MLIL_SET_VAR_SSA", writes=[callback])
    hook_call = Ins(2, "MLIL_CALL_SSA", reads=[callback])
    ssa = FakeSSA({callback: [hook_call]}, [callback_definition, hook_call])

    candidates, by_index = _candidate_slice(ssa, {1})
    kept = _drop_live_escapes(ssa, candidates, by_index)

    assert kept == set()


def test_non_ssa_root_does_not_pull_unrelated_same_index_ssa_instruction():
    wanted = Ins(10, "MLIL_SET_VAR_SSA", non_ssa=NonSSA(1))
    colliding_phi = Ins(1, "MLIL_VAR_PHI")

    candidates, _ = _candidate_slice(FakeSSA(instructions=[colliding_phi, wanted]), {1})

    assert candidates == {10}


def test_stack_var_state_write_keeps_source_live():
    x = Var("x", 1)
    state = Var("state", 1, VariableSourceType.StackVariableSourceType)

    decode = Ins(1, "MLIL_SET_VAR_SSA", writes=[x])
    state_write = Ins(2, "MLIL_SET_VAR_SSA", reads=[x], writes=[state])
    ssa = FakeSSA({x: [state_write]}, [decode, state_write])

    candidates, by_index = _candidate_slice(ssa, {1, 2})
    kept = _drop_live_escapes(ssa, candidates, by_index)

    assert kept == set()


def test_cleanup_decode_uses_phi_as_connector_but_never_nops_phi():
    a = Var("a", 1)
    b = Var("b", 1)

    decode = Ins(10, "MLIL_SET_VAR_SSA", writes=[a], non_ssa=NonSSA(1, 101))
    phi = Ins(20, "MLIL_VAR_PHI", reads=[a], writes=[b])
    mlil = FakeMLIL(FakeSSA({a: [phi], b: []}, [decode, phi]))

    assert phase_cleanup.cleanup_decode(mlil, {1, 20}, "call") == 1
    assert mlil.replaced == [(101, ("nop", ("loc", 101)))]
    assert mlil.finalized is True


def test_cleanup_settlement_replans_current_mlil_until_no_owned_assignment_remains(monkeypatch):
    first = NonSSA(1, 101)
    second = NonSSA(2, 102)
    assignments = [{1: first}, {2: second}, {}]
    mlil = FakeMLIL(FakeSSA())

    monkeypatch.setattr(
        phase_cleanup,
        "_cleanup_assignments",
        lambda *_args, **_kwargs: assignments.pop(0),
    )

    assert phase_cleanup.settle_cleanup_decode(mlil, {1, 2}, "branch") == (2, True)
    assert [expr_index for expr_index, _replacement in mlil.replaced] == [101, 102]


def test_cleanup_settlement_fails_closed_when_the_current_plan_repeats(monkeypatch):
    assignment = NonSSA(1, 101)
    assignments = [{1: assignment}, {1: assignment}]
    mlil = FakeMLIL(FakeSSA())

    monkeypatch.setattr(
        phase_cleanup,
        "_cleanup_assignments",
        lambda *_args, **_kwargs: assignments.pop(0),
    )

    assert phase_cleanup.settle_cleanup_decode(mlil, {1}, "branch") == (1, False)


def test_cleanup_settlement_bounds_nonrepeating_plans(monkeypatch):
    assignments = [
        {1: NonSSA(1, 101)},
        {2: NonSSA(2, 102)},
        {3: NonSSA(3, 103)},
    ]
    mlil = FakeMLIL(FakeSSA(), instruction_count=2)

    monkeypatch.setattr(
        phase_cleanup,
        "_cleanup_assignments",
        lambda *_args, **_kwargs: assignments.pop(0),
    )

    assert phase_cleanup.settle_cleanup_decode(mlil, {1}, "branch") == (2, False)
    assert [expr_index for expr_index, _replacement in mlil.replaced] == [101, 102]


def test_cleanup_decode_never_nops_faulting_or_unmodeled_assignments():
    loaded = Var("loaded", 1)
    unknown = Var("unknown", 1)
    load = Expr(M.MLIL_LOAD_SSA)
    unmodeled = Expr(M.MLIL_UNIMPL)
    load_definition = Ins(
        10,
        "MLIL_SET_VAR_SSA",
        writes=[loaded],
        non_ssa=NonSSA(1, 101, load),
        src=load,
    )
    unknown_definition = Ins(
        20,
        "MLIL_SET_VAR_SSA",
        writes=[unknown],
        non_ssa=NonSSA(2, 102, unmodeled),
        src=unmodeled,
    )
    mlil = FakeMLIL(FakeSSA({loaded: [], unknown: []}, [load_definition, unknown_definition]))

    assert phase_cleanup.cleanup_decode(mlil, {1, 2}, "branch") == 0
    assert mlil.replaced == []
    assert mlil.finalized is False


def test_cleanup_decode_nops_a_dead_load_only_with_an_explicit_phase_witness():
    loaded = Var("loaded", 1)
    load = Expr(M.MLIL_LOAD_SSA)
    definition = Ins(
        10,
        "MLIL_SET_VAR_SSA",
        writes=[loaded],
        non_ssa=NonSSA(1, 101, load),
        src=load,
    )
    mlil = FakeMLIL(FakeSSA({loaded: []}, [definition]))

    assert phase_cleanup.cleanup_decode(
        mlil,
        {1},
        "call",
        removable_load_roots={1},
    ) == 1
    assert mlil.replaced == [(101, ("nop", ("loc", 101)))]


def test_cleanup_decode_keeps_a_witnessed_load_with_a_real_external_use():
    loaded = Var("loaded", 1)
    load = Expr(M.MLIL_LOAD_SSA)
    definition = Ins(
        10,
        "MLIL_SET_VAR_SSA",
        writes=[loaded],
        non_ssa=NonSSA(1, 101, load),
        src=load,
    )
    callback_use = Ins(20, "MLIL_CALL_SSA", reads=[loaded])
    mlil = FakeMLIL(FakeSSA({loaded: [callback_use]}, [definition, callback_use]))

    assert phase_cleanup.cleanup_decode(
        mlil,
        {1},
        "call",
        removable_load_roots={1},
    ) == 0
    assert mlil.replaced == []


def test_cleanup_decode_keeps_trapping_division_but_nops_pure_arithmetic():
    divided = Var("divided", 1)
    decoded = Var("decoded", 1)
    division = Expr(M.MLIL_DIVU, (Expr(M.MLIL_VAR_SSA), Expr(M.MLIL_VAR_SSA)))
    arithmetic = Expr(M.MLIL_XOR, (Expr(M.MLIL_VAR_SSA), Expr(M.MLIL_CONST)))
    division_definition = Ins(
        10,
        "MLIL_SET_VAR_SSA",
        writes=[divided],
        non_ssa=NonSSA(1, 101, division),
        src=division,
    )
    arithmetic_definition = Ins(
        20,
        "MLIL_SET_VAR_SSA",
        writes=[decoded],
        non_ssa=NonSSA(2, 102, arithmetic),
        src=arithmetic,
    )
    mlil = FakeMLIL(FakeSSA({divided: [], decoded: []}, [division_definition, arithmetic_definition]))

    assert phase_cleanup.cleanup_decode(mlil, {1, 2}, "branch") == 1
    assert mlil.replaced == [(102, ("nop", ("loc", 102)))]


def test_cleanup_decode_treats_failed_use_query_as_live():
    decoded = Var("decoded", 1)
    definition = Ins(
        10,
        "MLIL_SET_VAR_SSA",
        writes=[decoded],
        non_ssa=NonSSA(1, 101),
    )

    class UnknownUses(FakeSSA):
        def get_ssa_var_uses(self, _var):
            raise RuntimeError("analysis unavailable")

    mlil = FakeMLIL(UnknownUses(instructions=[definition]))

    assert phase_cleanup.cleanup_decode(mlil, {1}, "branch") == 0
    assert mlil.replaced == []


if __name__ == "__main__":
    test_phi_only_use_does_not_keep_decode_candidate_live()
    test_phi_chain_with_real_use_keeps_decode_candidate_live()
    test_non_ssa_root_does_not_pull_unrelated_same_index_ssa_instruction()
    test_stack_var_state_write_keeps_source_live()
    test_cleanup_decode_uses_phi_as_connector_but_never_nops_phi()
