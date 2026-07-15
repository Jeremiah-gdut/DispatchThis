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


def test_external_correlated_provider_installs_the_atomically_rewritten_mlil(monkeypatch):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.workflow_state")
    function = Function()
    state = workflow_state.FunctionWorkflowState(function)
    state.mark_branch_stable()
    state.mark_call_stable()
    state.mark_global_stable()
    mlil = object()
    rewritten = object()
    plans = (
        semantics.CorrelatedStorePlan(
            store_il=object(),
            join_block=object(),
            size=4,
            arms=(
                semantics.CorrelatedStoreArm(
                    predecessor=object(),
                    incoming_edge=object(),
                    goto_il=object(),
                    dest_expr=object(),
                    dest_addr=0x1000,
                    src_expr=object(),
                    src_addr=0x2000,
                ),
                semantics.CorrelatedStoreArm(
                    predecessor=object(),
                    incoming_edge=object(),
                    goto_il=object(),
                    dest_expr=object(),
                    dest_addr=0x1004,
                    src_expr=object(),
                    src_addr=0x2004,
                ),
            ),
        ),
    )
    queries = []
    applied = []
    installed = []
    provider = semantics.SampleSemantics(
        provider_id="correlated-provider",
        name="Correlated provider",
        api_version=semantics.CORE_API_VERSION,
        correlated_stores=lambda query: (
            queries.append(query) or semantics.CompleteBatch(plans)
        ),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    monkeypatch.setattr(workflow, "_pending_reproof_functions", lambda _view: frozenset())
    monkeypatch.setattr(
        workflow,
        "apply_correlated_stores_mlil",
        lambda ctx, current, received: (
            applied.append((ctx, current, received)) or (rewritten, 1)
        ),
    )
    ctx = types.SimpleNamespace(
        function=function,
        view=types.SimpleNamespace(arch=types.SimpleNamespace(name="aarch64")),
        mlil=mlil,
        set_mlil_function=installed.append,
    )

    workflow.recover_phi_stores_mlil(ctx)

    assert [(query.view, query.function, query.mlil) for query in queries] == [
        (ctx.view, function, mlil)
    ]
    assert applied == [(ctx, mlil, plans)]
    assert installed == [rewritten]
