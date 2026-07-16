from complete_values_fakes import (
    Block,
    Edge,
    Expr,
    FakeSSA,
    Var,
    add,
    const,
    phi,
    reg,
    set_reg,
    values_module,
)


class _EdgeType:
    def __init__(self, name):
        self.name = name


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


def test_duplicate_ssa_condition_phis_do_not_cross_pair_opposite_arms():
    values = values_module()
    entry, first_true, first_false, first_join = (Block() for _ in range(4))
    second_branch, second_true, second_false, second_join = (
        Block() for _ in range(4)
    )
    true = _EdgeType("TrueBranch")
    false = _EdgeType("FalseBranch")
    Edge(entry, first_true, true)
    Edge(entry, first_false, false)
    Edge(first_true, first_join)
    Edge(first_false, first_join)
    Edge(first_join, second_branch)
    Edge(second_branch, second_true, true)
    Edge(second_branch, second_false, false)
    Edge(second_true, second_join)
    Edge(second_false, second_join)

    predicate = Var("w0", 1)
    first_flag, second_flag = Var("cond:0", 1), Var("cond:1", 1)
    def comparison():
        return Expr(
            "LLIL_CMP_NE",
            left=reg(predicate, size=4),
            right=const(0, size=4),
            size=4,
        )
    first_definition = Expr("LLIL_SET_FLAG_SSA", src=comparison())
    second_definition = Expr("LLIL_SET_FLAG_SSA", src=comparison())
    entry.instructions.append(
        Expr("LLIL_IF", condition=Expr("LLIL_FLAG_SSA", src=first_flag, size=0))
    )
    second_branch.instructions.append(
        Expr("LLIL_IF", condition=Expr("LLIL_FLAG_SSA", src=second_flag, size=0))
    )

    first_true_value, first_false_value, first_value = (
        Var("x0", version) for version in range(3)
    )
    second_true_value, second_false_value, second_value = (
        Var("x1", version) for version in range(3)
    )
    ssa = FakeSSA(
        {
            first_true_value: set_reg(const(0x100), first_true),
            first_false_value: set_reg(const(0x200), first_false),
            first_value: phi(first_true_value, first_false_value, block=first_join),
            second_true_value: set_reg(const(1), second_true),
            second_false_value: set_reg(const(2), second_false),
            second_value: phi(
                second_true_value, second_false_value, block=second_join
            ),
        },
        {first_flag: first_definition, second_flag: second_definition},
    )

    result = values.evaluate_values(
        None,
        ssa,
        add(reg(first_value), reg(second_value)),
        values.AnalysisBudget(node_limit=80, edge_limit=80),
    )

    assert result.values == (0x101, 0x202)


def test_zero_width_register_phi_defers_masking_to_its_typed_consumer():
    values = values_module()
    left, right, join = Block(), Block(), Block()
    Edge(left, join)
    Edge(right, join)
    left_value, right_value, merged = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    ssa = FakeSSA(
        {
            left_value: set_reg(const(0x40), left),
            right_value: set_reg(const(0x80), right),
            merged: phi(left_value, right_value, block=join, size=0),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        reg(merged, size=8),
        values.AnalysisBudget(node_limit=30, edge_limit=30),
    )

    assert type(result) is values.CompleteValues, getattr(result, "reason", None)
    assert result.values == (0x40, 0x80)


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


def test_forwarded_phi_operand_uses_the_only_redefinition_free_predecessor():
    values = values_module()
    origin, left, right, join = Block(), Block(), Block(), Block()
    left_edge = Edge(left, join)
    right_edge = Edge(right, join)
    Edge(origin, left)
    Edge(origin, right)
    forwarded, direct, merged = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    forwarded_definition = set_reg(
        const(1), origin, dest=forwarded, instr_index=0
    )
    direct_definition = set_reg(const(2), right, dest=direct, instr_index=0)
    origin.instructions.append(forwarded_definition)
    right.instructions.append(direct_definition)
    ssa = FakeSSA(
        {
            forwarded: forwarded_definition,
            direct: direct_definition,
            merged: phi(forwarded, direct, block=join),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        reg(merged),
        values.AnalysisBudget(node_limit=30, edge_limit=30),
    )

    assert result.values == (1, 2)
    assert {case.sources[0].edges[0] for case in result.cases} == {
        left_edge,
        right_edge,
    }


def test_one_phi_operand_can_prove_multiple_redefinition_free_predecessors():
    values = values_module()
    left, middle, forwarded, join = Block(), Block(), Block(), Block()
    left_edge = Edge(left, join)
    middle_edge = Edge(middle, join)
    forwarded_edge = Edge(forwarded, join)
    Edge(middle, forwarded)
    left_value, forwarded_value, merged = Var("x0", 0), Var("x0", 1), Var("x0", 2)
    left_definition = set_reg(
        const(1), left, dest=left_value, instr_index=0
    )
    forwarded_definition = set_reg(
        const(2), middle, dest=forwarded_value, instr_index=0
    )
    left.instructions.append(left_definition)
    middle.instructions.append(forwarded_definition)
    ssa = FakeSSA(
        {
            left_value: left_definition,
            forwarded_value: forwarded_definition,
            merged: phi(left_value, forwarded_value, block=join),
        }
    )

    result = values.evaluate_values(
        None,
        ssa,
        reg(merged),
        values.AnalysisBudget(node_limit=30, edge_limit=30),
    )

    sources = {case.value: case.sources for case in result.cases}
    assert result.values == (1, 2)
    assert {source.edges[0] for source in sources[1]} == {left_edge}
    assert {source.edges[0] for source in sources[2]} == {
        middle_edge,
        forwarded_edge,
    }


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
