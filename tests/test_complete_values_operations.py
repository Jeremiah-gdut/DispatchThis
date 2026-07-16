from types import SimpleNamespace

from binaryninja import RegisterValueType

from complete_values_fakes import Expr, FakeSSA, Var, const, reg, set_reg, values_module


def _evaluate(values, expression, policy=None):
    return values.evaluate_values(
        None,
        FakeSSA({}),
        expression,
        values.AnalysisBudget(node_limit=32, edge_limit=32),
        policy,
    )


def test_standard_values_preserve_expression_width_and_signed_comparisons():
    values = values_module()
    overflow = Expr(
        "LLIL_ADD",
        left=const(0xFF, size=1),
        right=const(2),
        size=1,
    )
    signed_less_than = Expr(
        "LLIL_CMP_SLT",
        left=const(0xFF, size=1),
        right=const(0, size=1),
        size=1,
    )

    assert _evaluate(values, overflow).values == (1,)
    assert _evaluate(values, signed_less_than).values == (1,)


def test_core_evaluates_standard_integer_operations_before_value_policy():
    values = values_module()
    calls = []

    def policy(_expression, _operands):
        calls.append(True)
        return values.Handled((0,))

    cases = (
        (
            Expr(
                "MLIL_DIVU",
                left=Expr("MLIL_CONST", constant=7),
                right=Expr("MLIL_CONST", constant=2),
            ),
            3,
        ),
        (
            Expr(
                "MLIL_DIVS",
                left=Expr("MLIL_CONST", constant=0xF8, size=1),
                right=Expr("MLIL_CONST", constant=2, size=1),
                size=1,
            ),
            0xFC,
        ),
        (
            Expr(
                "MLIL_MODS",
                left=Expr("MLIL_CONST", constant=0xF9, size=1),
                right=Expr("MLIL_CONST", constant=2, size=1),
                size=1,
            ),
            0xFF,
        ),
        (
            Expr(
                "MLIL_ASR",
                left=Expr("MLIL_CONST", constant=0x80, size=1),
                right=Expr("MLIL_CONST", constant=7, size=1),
                size=1,
            ),
            0xFF,
        ),
        (Expr("MLIL_NOT", src=Expr("MLIL_CONST", constant=0xF0, size=1), size=1), 0x0F),
        (
            Expr(
                "MLIL_ADD_OVERFLOW",
                left=Expr("MLIL_CONST", constant=0x7F, size=1),
                right=Expr("MLIL_CONST", constant=1, size=1),
                size=1,
            ),
            1,
        ),
        (
            Expr(
                "MLIL_ADD_OVERFLOW",
                left=Expr("MLIL_CONST", constant=0xFF, size=1),
                right=Expr("MLIL_CONST", constant=1, size=1),
                size=1,
            ),
            0,
        ),
    )

    for expression, expected in cases:
        assert _evaluate(values, expression, policy).values == (expected,)

    unsupported = _evaluate(
        values,
        Expr(
            "MLIL_MULU_DP",
            left=Expr("MLIL_CONST", constant=2),
            right=Expr("MLIL_CONST", constant=3),
        ),
        policy,
    )
    assert unsupported.reason == "unsupported standard operation MLIL_MULU_DP"
    assert calls == []


def test_known_bnil_operations_cannot_escape_to_value_policy_except_controlled_loads():
    values = values_module()
    calls = []

    def policy(expression, operands):
        calls.append((expression, operands))
        return values.Handled((0x1234,))

    unsupported = _evaluate(values, Expr("MLIL_UNDEF"), policy)
    load_expression = Expr("MLIL_LOAD", src=Expr("MLIL_CONST", constant=0x5000))
    load = _evaluate(
        values,
        load_expression,
        policy,
    )

    assert unsupported.reason == "unsupported standard operation MLIL_UNDEF"
    assert load.values == (0x1234,)
    assert calls == [(load_expression, ((0x5000,),))]


def test_mlil_ssa_field_extracts_the_exact_field_from_its_definition():
    values = values_module()
    source = Var("x9", 3)
    field = Expr(
        "MLIL_VAR_SSA_FIELD",
        src=source,
        offset=1,
        size=1,
    )

    result = values.evaluate_values(
        None,
        FakeSSA({source: set_reg(const(0x11223344, size=4))}),
        field,
        values.AnalysisBudget(node_limit=32, edge_limit=32),
    )

    assert result.values == (0x33,)


def test_vsa_proven_controlled_load_is_a_leaf_before_its_stack_pointer_is_evaluated():
    values = values_module()
    load = Expr("LLIL_LOAD_SSA", src=reg(Var("sp", 2)), size=8)
    load.possible_values = SimpleNamespace(
        type=RegisterValueType.ConstantValue,
        value=0x59,
    )

    result = _evaluate(values, load)

    assert result.values == (0x59,)


def test_policy_can_prove_a_controlled_load_before_its_stack_pointer_is_evaluated():
    values = values_module()
    load = Expr("LLIL_LOAD_SSA", src=reg(Var("sp", 2)), size=8)
    observed = []

    class Policy:
        def resolve_load(self, expression):
            observed.append(expression)
            return values.Handled((0x59,)) if expression is load else values.NotHandled()

        def __call__(self, _expression, _operands):
            return values.NotHandled()

    result = _evaluate(
        values,
        Expr("LLIL_ADD", left=load, right=const(1)),
        Policy(),
    )

    assert result.values == (0x5A,)
    assert observed == [load]


def test_value_policy_receives_complete_operands_and_cannot_fall_back_when_it_declines():
    values = values_module()
    source = Var("x0", 1)
    magic = Expr("LLIL_SAMPLE_DECODE", src=reg(source))
    observed = []

    def policy(expression, operands):
        observed.append((expression, operands))
        return values.Handled((operands[0][0] + 0x1000,))

    result = values.evaluate_values(
        None,
        FakeSSA({source: set_reg(const(0x20))}),
        magic,
        values.AnalysisBudget(node_limit=10, edge_limit=10),
        policy,
    )
    declined = values.evaluate_values(
        None,
        FakeSSA({source: set_reg(const(0x20))}),
        magic,
        values.AnalysisBudget(node_limit=10, edge_limit=10),
        lambda _expression, _operands: values.NotHandled(),
    )

    assert result.values == (0x1020,)
    assert observed == [(magic, ((0x20,),))]
    assert declined.reason == "unsupported operation LLIL_SAMPLE_DECODE"


def test_value_policy_inconclusive_discards_every_path():
    values = values_module()
    source = Var("x0", 1)
    magic = Expr("LLIL_SAMPLE_DECODE", src=reg(source))

    result = values.evaluate_values(
        None,
        FakeSSA({source: set_reg(const(0x20))}),
        magic,
        values.AnalysisBudget(node_limit=10, edge_limit=10),
        lambda _expression, _operands: values.Inconclusive("sample input missing"),
    )

    assert result.reason == "sample input missing"
