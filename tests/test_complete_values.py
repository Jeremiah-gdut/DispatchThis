from complete_values_fakes import FakeSSA, Var, add, const, reg, set_reg, values_module


def test_evaluate_values_uses_every_definition_without_a_hidden_depth_limit():
    values = values_module()
    variables = [Var("x0", index) for index in range(80)]
    definitions = {variables[0]: set_reg(const(0x41))}
    for previous, current in zip(variables, variables[1:]):
        definitions[current] = set_reg(reg(previous))

    result = values.evaluate_values(
        None,
        FakeSSA(definitions),
        reg(variables[-1]),
        values.AnalysisBudget(node_limit=200, edge_limit=200),
    )

    assert result.values == (0x41,)
    assert len(result.definition_graph.leaves) == 1
    assert len(result.definition_graph.edges) == 160


def test_evaluate_values_is_limited_by_its_explicit_budget_not_python_recursion():
    values = values_module()
    variables = [Var("x0", index) for index in range(1_200)]
    definitions = {variables[0]: set_reg(const(0x42))}
    for previous, current in zip(variables, variables[1:]):
        definitions[current] = set_reg(reg(previous))

    result = values.evaluate_values(
        None,
        FakeSSA(definitions),
        reg(variables[-1]),
        values.AnalysisBudget(node_limit=2_500, edge_limit=2_500),
    )

    assert type(result) is values.CompleteValues, getattr(result, "reason", None)
    assert result.values == (0x42,)


def test_definition_budgets_and_missing_definitions_fail_closed():
    values = values_module()
    source = Var("x0", 1)

    node_limited = values.evaluate_values(
        None,
        FakeSSA({source: set_reg(const(1))}),
        add(reg(source), const(2)),
        values.AnalysisBudget(node_limit=1, edge_limit=10),
    )
    edge_limited = values.evaluate_values(
        None,
        FakeSSA({source: set_reg(const(1))}),
        reg(source),
        values.AnalysisBudget(node_limit=10, edge_limit=1),
    )
    missing = values.evaluate_values(
        None,
        FakeSSA({}),
        reg(Var("x0", 1)),
        values.AnalysisBudget(node_limit=10, edge_limit=10),
    )

    assert node_limited.reason == "definition graph node budget exhausted"
    assert edge_limited.reason == "definition graph edge budget exhausted"
    assert missing.reason == "required SSA definition is unavailable"


def test_definition_cycle_is_inconclusive_without_a_partial_result():
    values = values_module()
    first = Var("first", 0)
    second = Var("second", 0)

    result = values.evaluate_values(
        None,
        FakeSSA(
            {
                first: set_reg(reg(second)),
                second: set_reg(reg(first)),
            }
        ),
        reg(first),
        values.AnalysisBudget(node_limit=16, edge_limit=16),
    )

    assert result.reason == "definition graph contains a cycle"


def test_variable_identity_never_falls_back_to_the_display_name():
    values = values_module()

    class SameDisplayName:
        __hash__ = object.__hash__

        def __str__(self):
            return "x0#1"

    first = SameDisplayName()
    second = SameDisplayName()
    result = values.evaluate_values(
        None,
        FakeSSA({first: set_reg(const(1)), second: set_reg(const(2))}),
        add(reg(first), reg(second)),
        values.AnalysisBudget(node_limit=20, edge_limit=20),
    )

    assert result.values == (3,)
