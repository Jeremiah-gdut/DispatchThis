from complete_values_fakes import (
    Block,
    Edge,
    FakeSSA,
    Var,
    add,
    const,
    phi,
    reg,
    set_reg,
    values_module,
)


def _sibling_phi_expression():
    left = Block()
    right = Block()
    join = Block()
    left_edge = Edge(left, join)
    right_edge = Edge(right, join)
    a0, a1, a = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    b0, b1, b = Var("x1", 0), Var("x1", 1), Var("x1", 2)
    ssa = FakeSSA(
        {
            a0: set_reg(const(1), left),
            a1: set_reg(const(2), right),
            a: phi(a0, a1, block=join),
            b0: set_reg(const(10), left),
            b1: set_reg(const(20), right),
            b: phi(b1, b0, block=join),
        }
    )
    return ssa, add(reg(a), reg(b)), {left_edge, right_edge}


def test_sibling_phi_values_follow_exact_raw_cfg_edges_not_operand_order():
    values = values_module()
    ssa, expression, expected_edges = _sibling_phi_expression()

    result = values.evaluate_values(
        None,
        ssa,
        expression,
        values.AnalysisBudget(node_limit=50, edge_limit=50),
    )

    assert result.values == (11, 22)
    assert tuple(len(case.sources) for case in result.cases) == (1, 1)
    assert {case.sources[0].edges[0] for case in result.cases} == expected_edges


def test_forwarded_phi_operand_without_a_direct_edge_owner_is_inconclusive():
    values = values_module()
    origin, left, right, join = Block(), Block(), Block(), Block()
    Edge(origin, left)
    Edge(origin, right)
    Edge(left, join)
    Edge(right, join)
    forwarded, direct, merged = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    ssa = FakeSSA(
        {
            forwarded: set_reg(const(1), origin),
            direct: set_reg(const(2), right),
            merged: phi(forwarded, direct, block=join),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        reg(merged),
        values.AnalysisBudget(node_limit=30, edge_limit=30),
    )

    assert (
        result.reason == "phi operands cannot be uniquely matched to incoming CFG edges"
    )


def test_sibling_phi_ambiguity_is_inconclusive_instead_of_guessing_an_edge():
    values = values_module()
    source = Block()
    join = Block()
    Edge(source, join)
    Edge(source, join)
    left, right, merged = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    ssa = FakeSSA(
        {
            left: set_reg(const(1), source),
            right: set_reg(const(2), source),
            merged: phi(left, right, block=join),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        reg(merged),
        values.AnalysisBudget(node_limit=20, edge_limit=20),
    )

    assert (
        result.reason == "phi operands cannot be uniquely matched to incoming CFG edges"
    )


def test_cross_join_phi_values_without_a_shared_cfg_path_are_inconclusive():
    values = values_module()
    a_left, a_right, a_join = Block(), Block(), Block()
    b_left, b_right, b_join = Block(), Block(), Block()
    final = Block()
    Edge(a_left, a_join)
    Edge(a_right, a_join)
    Edge(b_left, b_join)
    Edge(b_right, b_join)
    Edge(a_join, final)
    Edge(b_join, final)
    a0, a1, a = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    b0, b1, b = Var("x1", 0), Var("x1", 1), Var("x1", 2)
    ssa = FakeSSA(
        {
            a0: set_reg(const(1), a_left),
            a1: set_reg(const(2), a_right),
            a: phi(a0, a1, block=a_join),
            b0: set_reg(const(10), b_left),
            b1: set_reg(const(20), b_right),
            b: phi(b0, b1, block=b_join),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        add(reg(a), reg(b)),
        values.AnalysisBudget(node_limit=80, edge_limit=80),
    )

    assert result.reason == "no CFG-compatible path evaluates expression"


def test_duplicate_values_keep_every_raw_path_source_after_target_deduplication():
    values = values_module()
    a_left, a_right, a_join = Block(), Block(), Block()
    a_left_edge, a_right_edge = Edge(a_left, a_join), Edge(a_right, a_join)
    b_left, b_right, b_join = Block(), Block(), Block()
    Edge(a_join, b_left)
    Edge(a_join, b_right)
    b_left_edge, b_right_edge = Edge(b_left, b_join), Edge(b_right, b_join)
    a0, a1, a = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    b0, b1, b = Var("x1", 0), Var("x1", 1), Var("x1", 2)
    ssa = FakeSSA(
        {
            a0: set_reg(const(1), a_left),
            a1: set_reg(const(2), a_right),
            a: phi(a0, a1, block=a_join),
            b0: set_reg(const(10), b_left),
            b1: set_reg(const(10), b_right),
            b: phi(b0, b1, block=b_join),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        add(reg(a), reg(b)),
        values.AnalysisBudget(node_limit=80, edge_limit=80),
    )

    observed_edges = {
        edge
        for case in result.cases
        for source in case.sources
        for edge in source.edges
    }
    assert result.values == (11, 12)
    assert tuple(len(case.sources) for case in result.cases) == (2, 2)
    assert observed_edges == {a_left_edge, a_right_edge, b_left_edge, b_right_edge}
