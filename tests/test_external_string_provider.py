import sys
import types

from conftest import load_plugin_module


semantics = load_plugin_module("plugins.DispatchThis.semantics")


class View:
    def __init__(self):
        self.arch = types.SimpleNamespace(name="aarch64")
        self.session_data = {
            "dispatchthis_mlil_stable": {
                0x2000: True,
                0x3000: False,
                "invalid": True,
            }
        }


class Function:
    name = "sub_4000"
    start = 0x4000
    indirect_branches = []
    unresolved_indirect_branches = []

    def __init__(self):
        self.session_data = {}
        self.comments = {
            0x5000: "manual note\n[decrypt] manually written text",
        }

    def get_comment_at(self, address):
        return self.comments.get(address, "")

    def set_comment_at(self, address, comment):
        self.comments[address] = comment


def _stable_state(workflow_state, function):
    state = workflow_state.FunctionWorkflowState(function)
    state.mark_branch_stable()
    state.mark_call_stable()
    state.mark_global_stable()
    return state


def test_external_string_provider_uses_frozen_gate_and_applies_a_partial_batch(monkeypatch):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    load_plugin_module("plugins.DispatchThis.passes.medium.string_decrypt")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.workflow_state")
    view = View()
    function = Function()
    _stable_state(workflow_state, function)
    queries = []
    provider = semantics.SampleSemantics(
        provider_id="external-strings",
        name="External strings",
        api_version=semantics.CORE_API_VERSION,
        string_recovery=lambda query: (
            queries.append(query)
            or semantics.CompleteBatch(
                (
                    semantics.StringRecoveryFact(
                        call_addr=0x5000,
                        source_addr=0x7000,
                        destination_addr=0x6000,
                        plaintext=b'A\x00\n\\\x80Z',
                    ),
                )
            )
        ),
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    context = types.SimpleNamespace(function=function, view=view, mlil=object())

    assert workflow.string_decrypt_mlil(context) == 1
    assert len(queries) == 1
    assert queries[0].deflattened_function_starts == frozenset({0x2000})
    assert function.comments == {
        0x5000: (
            "manual note\n"
            "[decrypt] manually written text\n"
            "[DispatchThis decrypt] A\\0\\n\\\\\\x80Z, src=0x7000 dst=0x6000"
        )
    }


def test_provider_can_skip_unsupported_calls_without_blocking_a_proven_fact(monkeypatch):
    sys.modules["plugins.DispatchThis.semantics"] = semantics
    load_plugin_module("plugins.DispatchThis.passes.medium.string_decrypt")
    workflow = load_plugin_module("plugins.DispatchThis.workflow")
    workflow_state = load_plugin_module("plugins.DispatchThis.workflow_state")
    view = View()
    function = Function()
    _stable_state(workflow_state, function)
    observed_candidates = []

    def recover(query):
        facts = []
        for candidate in query.mlil.calls:
            observed_candidates.append(candidate.kind)
            if candidate.kind != "proven":
                continue
            facts.append(
                semantics.StringRecoveryFact(0x5010, 0x7010, 0x6010, b"recovered")
            )
        return semantics.CompleteBatch(tuple(facts))

    provider = semantics.SampleSemantics(
        provider_id="partial-strings",
        name="Partial strings",
        api_version=semantics.CORE_API_VERSION,
        string_recovery=recover,
    )
    monkeypatch.setattr(workflow, "active_provider", lambda _view: provider)
    mlil = types.SimpleNamespace(calls=(
        types.SimpleNamespace(kind="indirect"),
        types.SimpleNamespace(kind="multi-target"),
        types.SimpleNamespace(kind="unsupported"),
        types.SimpleNamespace(kind="proven"),
    ))
    context = types.SimpleNamespace(function=function, view=view, mlil=mlil)

    assert workflow.string_decrypt_mlil(context) == 1
    assert observed_candidates == ["indirect", "multi-target", "unsupported", "proven"]
    assert function.comments[0x5010] == (
        "[DispatchThis decrypt] recovered, src=0x7010 dst=0x6010"
    )
