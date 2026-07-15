import inspect
import json
import types
from importlib import import_module

import pytest


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}
        self.writes = []
        self.groups = []
        self.settings = []

    def register_group(self, group, title):
        self.groups.append((group, title))
        return True

    def register_setting(self, key, properties):
        self.settings.append((key, json.loads(properties)))
        return True

    def get_string(self, key, resource=None):
        return self.values.get((key, resource))

    def set_string(self, key, value, resource=None, scope=None):
        self.writes.append((key, value, resource, scope))
        self.values[(key, resource)] = value
        return True


def test_default_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("default")

    assert profile.id == "default"
    assert profile.name
    assert profile.description
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.correlated_stores is None
    assert profile.plan_deflatten_redirections is not None


def test_dyzznb_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("dyzznb")

    assert profile.id == "dyzznb"
    assert profile.name == "DYZZNB"
    assert profile.description
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.correlated_stores is None
    assert profile.plan_deflatten_redirections is not None


def test_valorant_2_6_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("valorant_2_6")

    assert profile.id == "valorant_2_6"
    assert profile.name == "Valorant 2.6"
    assert profile.description
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.correlated_stores is not None
    assert profile.plan_deflatten_redirections is not None


def test_driver_2_6_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("driver_2_6")

    assert profile.id == "driver_2_6"
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.correlated_stores is None
    assert profile.plan_deflatten_redirections is not None


def test_profile_missing_hook_defaults_to_no_recovery():
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="branch_only",
        PROFILE_NAME="Branch only",
        PROFILE_DESCRIPTION="Only resolves branch gadgets",
        resolve_branch_gadget=lambda *_args: [],
    )

    profile = profiles.resolver_profile_from_module(module)

    assert profile.resolve_branch_gadget(None, None, None) == []
    assert profile.resolve_call_gadget(None, None) == []
    assert profile.plan_global_constant_slots(None, None) == []
    assert profile.correlated_stores is None
    assert profile.plan_deflatten_redirections(None, None, None) == []


def test_profile_rejects_present_noncallable_hook():
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="broken",
        PROFILE_NAME="Broken",
        PROFILE_DESCRIPTION="Invalid call hook",
        resolve_call_gadget=None,
    )

    with pytest.raises(profiles.InvalidResolverProfile, match="resolve_call_gadget"):
        profiles.resolver_profile_from_module(module)


def test_explicit_noop_hooks_are_valid():
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="noop",
        PROFILE_NAME="No-op",
        PROFILE_DESCRIPTION="Valid empty profile",
        resolve_branch_gadget=lambda *_args: [],
        resolve_call_gadget=lambda *_args: [],
        plan_global_constant_slots=lambda *_args: [],
        plan_deflatten_redirections=lambda *_args: [],
    )

    profile = profiles.resolver_profile_from_module(module)

    assert profile.id == "noop"
    assert profile.resolve_branch_gadget(None, None, None) == []
    assert profile.resolve_call_gadget(None, None, None) == []
    assert profile.plan_global_constant_slots(None, None) == []
    assert profile.correlated_stores is None
    assert profile.plan_deflatten_redirections(None, None, None) == []


def test_dyzznb_profile_delegates_to_existing_planners(monkeypatch):
    profiles = import_module("plugins.DispatchThis.profiles")
    dyzznb = import_module("plugins.DispatchThis.profiles.dyzznb")

    monkeypatch.setattr(dyzznb, "resolve_llil_jump_plan", lambda *args: ("branch", args))
    monkeypatch.setattr(dyzznb, "plan_indirect_calls", lambda *args: ("call", args))
    monkeypatch.setattr(dyzznb, "compute_redirections", lambda *args, **kwargs: ("deflatten", args, kwargs))

    profile = profiles.resolver_profile_from_module(dyzznb)

    assert profile.resolve_branch_gadget("bv", "llil", {"known": "targets"}) == (
        "branch",
        ("bv", "llil", {"known": "targets"}),
    )
    assert profile.resolve_call_gadget("bv", "mlil") == ("call", ("bv", "mlil"))
    assert profile.correlated_stores is None
    assert profile.plan_deflatten_redirections("bv", "func", "mlil") == (
        "deflatten",
        ("bv", "func"),
        {"mlil": "mlil"},
    )


def test_default_profile_keeps_dyzznb_callables_for_existing_views():
    profiles = import_module("plugins.DispatchThis.profiles")
    default = profiles.get_profile("default")
    dyzznb = profiles.get_profile("dyzznb")

    for hook in profiles.PROFILE_HOOKS:
        assert getattr(default, hook) is getattr(dyzznb, hook)


def test_compatibility_and_delegating_profiles_do_not_import_pass_planners():
    modules = (
        import_module("plugins.DispatchThis.profiles.default"),
        import_module("plugins.DispatchThis.profiles.driver_2_6"),
        import_module("plugins.DispatchThis.profiles.valorant_2_6"),
    )

    for module in modules:
        source = inspect.getsource(module)
        assert "passes." not in source
        assert "..passes" not in source


def test_profile_setting_is_registered_with_bundled_profiles():
    profiles = import_module("plugins.DispatchThis.profiles")
    settings = FakeSettings()

    assert profiles.register_profile_settings(settings=settings)

    assert settings.groups == [("analysis.plugins.dispatchThis", "DispatchThis")]
    assert settings.settings == [(
        profiles.ACTIVE_PROFILE_SETTING,
        {
            "title": "Resolver Profile",
            "description": "Active DispatchThis resolver profile for this BinaryView.",
            "type": "string",
            "default": "default",
            "enum": ["default", "driver_2_6", "dyzznb", "valorant_2_6"],
        },
    )]


def test_active_profile_defaults_to_default():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.active_profile("bv", settings=FakeSettings())

    assert profile.id == "default"


def test_active_profile_uses_configured_profile(monkeypatch):
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="configured",
        PROFILE_NAME="Configured",
        PROFILE_DESCRIPTION="Fake configured profile",
        resolve_branch_gadget=lambda *_args: [],
        resolve_call_gadget=lambda *_args: [],
        plan_global_constant_slots=lambda *_args: [],
        plan_deflatten_redirections=lambda *_args: [],
    )
    configured = profiles.resolver_profile_from_module(module)
    monkeypatch.setitem(profiles._PROFILES, configured.id, configured)
    settings = FakeSettings({(profiles.ACTIVE_PROFILE_SETTING, "bv"): "configured"})

    profile = profiles.active_profile("bv", settings=settings)

    assert profile is configured


def test_active_profile_rejects_unknown_setting_and_warns(monkeypatch):
    profiles = import_module("plugins.DispatchThis.profiles")
    warnings = []
    settings = FakeSettings({(profiles.ACTIVE_PROFILE_SETTING, "bv"): "missing"})
    monkeypatch.setattr(profiles, "log_warn", warnings.append)

    with pytest.raises(profiles.InvalidResolverProfile, match="missing"):
        profiles.active_profile("bv", settings=settings)

    assert warnings == [
        "[profiles] unknown resolver profile 'missing'; refusing resolver work"
    ]


def test_setting_active_profile_only_writes_binaryview_profile_setting():
    profiles = import_module("plugins.DispatchThis.profiles")
    settings = FakeSettings()

    assert profiles.set_active_profile("bv", "dyzznb", settings=settings)

    assert settings.writes == [(
        profiles.ACTIVE_PROFILE_SETTING,
        "dyzznb",
        "bv",
        profiles.SettingsScope.SettingsResourceScope,
    )]
