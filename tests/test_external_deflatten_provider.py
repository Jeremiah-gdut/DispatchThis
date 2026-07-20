import sys
import types

from conftest import load_plugin_module


semantics = load_plugin_module("plugins.DispatchThis.semantics")


class Function:
    name = "sub_4000"
    start = 0x4000

    def __init__(self):
        self.session_data = {}
        self.indirect_branches = []
        self.unresolved_indirect_branches = []


def test_deflatten_provider_installs_atomically_rewritten_mlil_when_ready(monkeypatch):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.state")

    # Given: all prerequisite phases are current and one provider plan exists.
    function = Function()
    state = workflow_state.FunctionWorkflowState(function)
    state.mark_branch_stable()
    state.mark_call_stable()
    state.mark_global_stable()
    state.mark_branch_cleanup_done()
    state.mark_call_cleanup_done()
    plan = semantics.DeflattenPlan(
        kind=semantics.DeflattenPlanKind.UNCONDITIONAL,
        owner_block=object(),
        exit_redirections=(semantics.DeflattenRedirection(object(), 0x2000),),
        state_token=semantics.DeflattenStateToken(0x1234, 4),
    )
    query_calls = []
    applied = []
    installed = []
    provider = semantics.SampleSemantics(
        provider_id="deflatten-provider",
        name="Deflatten provider",
        api_version=semantics.CORE_API_VERSION,
        deflatten=lambda query: query_calls.append(query) or semantics.CompleteBatch((plan,)),
    )
    view = types.SimpleNamespace(session_data={}, arch=types.SimpleNamespace(name="aarch64"))
    mlil = object()
    rewritten = object()
    context = types.SimpleNamespace(
        function=function,
        view=view,
        mlil=mlil,
        set_mlil_function=installed.append,
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    monkeypatch.setattr(workflow, "_pending_reproof_functions", lambda _view: frozenset())
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _function: True)
    monkeypatch.setattr(
        workflow,
        "rewrite_redirections_mlil",
        lambda ctx, current, plans: applied.append((ctx, current, plans)) or (rewritten, len(plans)),
    )

    # When: the deflatten workflow activity runs.
    workflow.deflatten_mlil(context)

    # Then: only the provider plan is sent to the atomic core backend and committed.
    assert [(query.view, query.function, query.mlil) for query in query_calls] == [
        (view, function, mlil)
    ]
    assert applied == [(context, mlil, (plan,))]
    assert installed == [rewritten]
    assert view.session_data["dispatchthis_mlil_stable"][function.start] is True


def test_deflatten_provider_rejects_an_untyped_batch_before_rewrite(monkeypatch):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.state")
    function = Function()
    state = workflow_state.FunctionWorkflowState(function)
    state.mark_branch_stable()
    state.mark_call_stable()
    state.mark_global_stable()
    state.mark_branch_cleanup_done()
    state.mark_call_cleanup_done()
    provider = semantics.SampleSemantics(
        provider_id="untyped-deflatten-provider",
        name="Untyped deflatten provider",
        api_version=semantics.CORE_API_VERSION,
        deflatten=lambda _query: semantics.CompleteBatch(({"kind": "uncond"},)),
    )
    applied = []
    context = types.SimpleNamespace(
        function=function,
        view=types.SimpleNamespace(session_data={}, arch=types.SimpleNamespace(name="aarch64")),
        mlil=object(),
        set_mlil_function=lambda _mlil: None,
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    monkeypatch.setattr(workflow, "_pending_reproof_functions", lambda _view: frozenset())
    monkeypatch.setattr(workflow, "_ensure_analysis_settings", lambda _function: True)
    monkeypatch.setattr(
        workflow,
        "rewrite_redirections_mlil",
        lambda *_args: applied.append(_args) or (object(), 1),
    )

    workflow.deflatten_mlil(context)

    assert applied == []
    assert "dispatchthis_mlil_stable" not in context.view.session_data
