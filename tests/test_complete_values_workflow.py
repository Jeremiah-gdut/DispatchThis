"""Integration coverage for a sample policy used by the branch workflow."""

from __future__ import annotations

import types

from complete_values_fakes import Expr, FakeSSA, Var, const, reg, set_reg, values_module

from conftest import load_plugin_module


class _Branch:
    def __init__(self, source_addr, dest_addr):
        self.source_addr = source_addr
        self.dest_addr = dest_addr
        self.auto_defined = False


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
        self.indirect_branches = [_Branch(source, target) for _arch, target in targets]


def test_sample_value_policy_can_prove_a_branch_target_through_the_real_workflow(
    monkeypatch,
):
    values = values_module()
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    source = Var("x0", 1)
    destination = Expr("LLIL_SAMPLE_DECODE", src=reg(source), expr_index=7)
    jump = Expr(
        "LLIL_JUMP",
        address=0x1000,
        dest=destination,
        expr_index=3,
        instr_index=3,
    )
    function = _Function()
    view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )
    llil = types.SimpleNamespace()
    seen = []
    warnings = []

    def policy(expression, operands):
        seen.append((expression, operands))
        if expression is destination:
            return values.Handled((operands[0][0] + 0x1000,))
        return values.NotHandled()

    def branch_targets(query):
        result = values.evaluate_values(
            query.view,
            FakeSSA({source: set_reg(const(0x20))}),
            jump.dest,
            values.AnalysisBudget(node_limit=20, edge_limit=20),
            policy,
        )
        if type(result) is values.Inconclusive:
            return semantics.Inconclusive(result.reason)
        return semantics.CompleteBatch(
            (semantics.BranchTargetFact(jump, result.values),)
        )

    provider = semantics.SampleSemantics(
        provider_id="sample-values",
        name="Sample values",
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
    monkeypatch.setattr(workflow, "log_warn", warnings.append)
    monkeypatch.setattr(
        workflow, "validate_current_branch_plans", lambda _view, _llil, plans: plans
    )
    monkeypatch.setattr(workflow, "apply_llil_jump_rewrites", lambda *_args: 0)

    workflow.resolve_jumps_llil(
        types.SimpleNamespace(function=function, view=view, llil=llil)
    )

    assert seen == [(destination, ((0x20,),))]
    assert warnings == []
    assert function.submitted == [(0x1000, ((view.arch, 0x1020),))]


def test_inconclusive_value_policy_leaves_the_branch_site_unresolved(monkeypatch):
    values = values_module()
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    source = Var("x0", 1)
    destination = Expr("LLIL_SAMPLE_DECODE", src=reg(source), expr_index=7)
    jump = Expr(
        "LLIL_JUMP",
        address=0x1000,
        dest=destination,
        expr_index=3,
        instr_index=3,
    )
    function = _Function()
    view = types.SimpleNamespace(
        arch=types.SimpleNamespace(name="aarch64"),
        session_data={},
    )

    def branch_targets(query):
        result = values.evaluate_values(
            query.view,
            FakeSSA({source: set_reg(const(0x20))}),
            jump.dest,
            values.AnalysisBudget(node_limit=20, edge_limit=20),
            lambda _expression, _operands: values.Inconclusive("sample input missing"),
        )
        return semantics.Inconclusive(result.reason)

    provider = semantics.SampleSemantics(
        provider_id="sample-values-failed",
        name="Sample values failed",
        api_version=semantics.CORE_API_VERSION,
        branch_targets=branch_targets,
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    monkeypatch.setattr(workflow, "_legacy_profile", lambda _provider_id: None)
    monkeypatch.setattr(
        workflow, "_pending_reproof_functions", lambda _view: frozenset()
    )
    monkeypatch.setattr(workflow, "iter_llil_indirect_jumps", lambda _llil: (jump,))
    monkeypatch.setattr(workflow, "log_warn", lambda _message: None)

    workflow.resolve_jumps_llil(
        types.SimpleNamespace(
            function=function, view=view, llil=types.SimpleNamespace()
        )
    )

    assert function.submitted == []
    assert (
        function.session_data["dispatchthis_workflow_state"]["branch"]["receipts"] == {}
    )
