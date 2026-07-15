"""Branch-workflow integration coverage for path-correlated PHI values."""

from __future__ import annotations

import types

from complete_values_fakes import (
    Block,
    Edge,
    Expr,
    FakeSSA,
    Var,
    const,
    phi,
    reg,
    set_reg,
    values_module,
)

from conftest import load_plugin_module


class _Function:
    name = "sub_1000"
    start = 0x1000

    def __init__(self):
        self.session_data = {}
        self.indirect_branches = []
        self.unresolved_indirect_branches = []
        self.submitted = []

    def set_user_indirect_branches(self, source, targets):
        self.submitted.append((source, tuple(targets)))
        self.indirect_branches = [
            types.SimpleNamespace(
                source_addr=source,
                dest_addr=target,
                auto_defined=False,
            )
            for _arch, target in targets
        ]


def _resolve_with_provider(monkeypatch, values, jump, resolve_values):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    function = _Function()
    view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )

    def branch_targets(query):
        result = resolve_values(query)
        if type(result) is values.Inconclusive:
            return semantics.Inconclusive(result.reason)
        return semantics.CompleteBatch(
            (semantics.BranchTargetFact(jump, result.values),)
        )

    provider = semantics.SampleSemantics(
        provider_id="phi-values",
        name="PHI values",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=branch_targets,
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    monkeypatch.setattr(workflow, "_legacy_profile", lambda _provider_id: None)
    monkeypatch.setattr(
        workflow, "_pending_reproof_functions", lambda _view: frozenset()
    )
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _function: True)
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(
        workflow, "validate_current_branch_plans", lambda _view, _llil, plans: plans
    )
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)
    monkeypatch.setattr(workflow, "log_warn", lambda _message: None)
    workflow.resolve_jumps_llil(
        types.SimpleNamespace(
            function=function, view=view, llil=types.SimpleNamespace()
        )
    )
    return function, view


def test_swapped_sibling_phi_targets_are_submitted_without_cross_product_edges(
    monkeypatch,
):
    values = values_module()
    left, right, join = Block(), Block(), Block()
    Edge(left, join)
    Edge(right, join)
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
    destination = Expr("LLIL_ADD", left=reg(a), right=reg(b), expr_index=7)
    jump = Expr("LLIL_JUMP", address=0x1000, dest=destination, expr_index=3)

    function, view = _resolve_with_provider(
        monkeypatch,
        values,
        jump,
        lambda query: values.evaluate_values(
            query.view,
            ssa,
            destination,
            values.AnalysisBudget(node_limit=50, edge_limit=50),
        ),
    )

    assert function.submitted == [(0x1000, ((view.arch, 11), (view.arch, 22)))]


def test_ambiguous_phi_ownership_leaves_the_branch_site_unresolved(monkeypatch):
    values = values_module()
    source, join = Block(), Block()
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
    destination = Expr("LLIL_REG_SSA", src=merged, expr_index=7)
    jump = Expr("LLIL_JUMP", address=0x1000, dest=destination, expr_index=3)

    function, _view = _resolve_with_provider(
        monkeypatch,
        values,
        jump,
        lambda query: values.evaluate_values(
            query.view,
            ssa,
            destination,
            values.AnalysisBudget(node_limit=30, edge_limit=30),
        ),
    )

    assert function.submitted == []
    assert (
        function.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}
    )
