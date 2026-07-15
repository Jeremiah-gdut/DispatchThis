import pytest

from conftest import load_plugin_module


def _provider(semantics, provider_id, api_version=None):
    return semantics.SampleSemantics(
        provider_id=provider_id,
        name="External test sample",
        api_version=semantics.CORE_API_VERSION if api_version is None else api_version,
        branch_targets=lambda _query: semantics.CompleteBatch(()),
    )


def test_external_provider_registers_one_empty_branch_slot():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    providers = load_plugin_module("plugins.DispatchThis.providers")
    provider = _provider(semantics, "external-empty-slot")

    assert providers.register_provider(provider)
    assert providers.get_provider("external-empty-slot") is provider
    assert provider.branch_targets is not None
    assert provider.branch_targets(None) == semantics.CompleteBatch(())


def test_provider_registry_rejects_version_mismatch_and_id_conflict_once(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    providers = load_plugin_module("plugins.DispatchThis.providers")
    warnings = []
    monkeypatch.setattr(providers, "log_warn", warnings.append)

    wrong_version = _provider(semantics, "external-wrong-version", api_version=0)
    assert not providers.register_provider(wrong_version)
    assert not providers.register_provider(wrong_version)
    assert providers.get_provider("external-wrong-version") is None

    provider = _provider(semantics, "external-duplicate")
    duplicate = _provider(semantics, "external-duplicate")
    assert providers.register_provider(provider)
    assert not providers.register_provider(duplicate)
    assert providers.get_provider("external-duplicate") is provider

    assert len(warnings) == 2
    assert "API version" in warnings[0]
    assert "duplicate" in warnings[1]


def test_complete_batch_and_inconclusive_have_distinct_contracts():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")

    complete = semantics.CompleteBatch(())
    inconclusive = semantics.Inconclusive("definition graph incomplete")

    assert complete.facts == ()
    assert inconclusive.reason == "definition graph incomplete"
    assert complete != inconclusive


def test_branch_fact_requires_a_nonempty_canonical_target_tuple():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    jump = type("Jump", (), {})()

    with pytest.raises(ValueError):
        semantics.BranchTargetFact(jump_il=jump, targets=())
    with pytest.raises(ValueError):
        semantics.BranchTargetFact(jump_il=jump, targets=(0x3000, 0x2000))


def test_degenerate_conditional_branch_fact_must_be_an_unconditional_single_target():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    jump = type("Jump", (), {})()

    unconditional = semantics.BranchTargetFact(jump_il=jump, targets=(0x2000,))

    assert unconditional.condition is None
    assert unconditional.true_target is None
    assert unconditional.false_target is None
    with pytest.raises(ValueError, match="distinct branch arms"):
        semantics.BranchTargetFact(
            jump_il=jump,
            targets=(0x2000,),
            condition=object(),
            true_target=0x2000,
            false_target=0x2000,
        )


def test_correlated_store_plan_requires_explicit_path_and_value_witnesses():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    arm = semantics.CorrelatedStoreArm(
        predecessor=object(),
        incoming_edge=object(),
        goto_il=object(),
        dest_expr=object(),
        dest_addr=0x1000,
        src_expr=object(),
        src_addr=0x2000,
    )
    plan = semantics.CorrelatedStorePlan(
        store_il=object(),
        join_block=object(),
        size=4,
        arms=(arm, arm),
    )

    assert plan.arms == (arm, arm)
    with pytest.raises(ValueError, match="two arms"):
        semantics.CorrelatedStorePlan(
            store_il=object(),
            join_block=object(),
            size=4,
            arms=(arm, object()),
        )


def test_legacy_profile_adapter_exposes_only_typed_correlated_store_batches():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    providers = load_plugin_module("plugins.DispatchThis.providers")
    profile = type(
        "Profile",
        (),
        {
            "id": "legacy-correlated-typed",
            "name": "Legacy correlated typed",
            "resolve_branch_gadget": staticmethod(lambda *_args: []),
            "correlated_stores": staticmethod(lambda _query: semantics.CompleteBatch(())),
        },
    )()

    assert providers._register_legacy_profile(profile)
    provider = providers.get_provider(profile.id)

    assert provider.correlated_stores is not None
    assert provider.correlated_stores(object()) == semantics.CompleteBatch(())


def test_active_provider_never_falls_back_to_a_registered_provider():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    providers = load_plugin_module("plugins.DispatchThis.providers")
    provider = _provider(semantics, "explicit-only")
    assert providers.register_provider(provider)

    class Settings:
        def get_string(self, _key, _resource):
            return ""

    with pytest.raises(providers.ProviderBindingError, match="no DispatchThis provider"):
        providers.active_provider("view", Settings())
