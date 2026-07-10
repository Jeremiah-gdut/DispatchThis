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
    assert profile.plan_correlated_store_rewrites is not None
    assert profile.plan_deflatten_redirections is not None
    assert profile.plan_string_decrypt_calls is not None


def test_dyzznb_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("dyzznb")

    assert profile.id == "dyzznb"
    assert profile.name == "DYZZNB"
    assert profile.description
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.plan_correlated_store_rewrites is not None
    assert profile.plan_deflatten_redirections is not None
    assert profile.plan_string_decrypt_calls is not None


def test_valorant_2_6_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("valorant_2_6")

    assert profile.id == "valorant_2_6"
    assert profile.name == "Valorant 2.6"
    assert profile.description
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.plan_correlated_store_rewrites is not None
    assert profile.plan_deflatten_redirections is not None
    assert profile.plan_string_decrypt_calls is not None


def test_driver_2_6_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("driver_2_6")

    assert profile.id == "driver_2_6"
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None
    assert profile.plan_correlated_store_rewrites is not None
    assert profile.plan_deflatten_redirections is not None
    assert profile.plan_string_decrypt_calls is not None


def test_profile_without_required_hook_is_rejected():
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="broken",
        PROFILE_NAME="Broken",
        PROFILE_DESCRIPTION="Missing call hook",
        resolve_branch_gadget=lambda *_args: [],
        plan_global_constant_slots=lambda *_args: [],
        plan_deflatten_redirections=lambda *_args: [],
        plan_string_decrypt_calls=lambda *_args: [],
    )

    with pytest.raises(profiles.InvalidResolverProfile):
        profiles.resolver_profile_from_module(module)


def test_noop_required_hooks_are_valid():
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="noop",
        PROFILE_NAME="No-op",
        PROFILE_DESCRIPTION="Valid empty profile",
        resolve_branch_gadget=lambda *_args: [],
        resolve_call_gadget=lambda *_args: [],
        plan_global_constant_slots=lambda *_args: [],
        plan_correlated_store_rewrites=lambda *_args: [],
        plan_deflatten_redirections=lambda *_args: [],
        plan_string_decrypt_calls=lambda *_args: [],
    )

    profile = profiles.resolver_profile_from_module(module)

    assert profile.id == "noop"
    assert profile.resolve_branch_gadget(None, None, None) == []
    assert profile.resolve_call_gadget(None, None, None) == []
    assert profile.plan_global_constant_slots(None, None) == []
    assert profile.plan_correlated_store_rewrites(None, None, None) == []
    assert profile.plan_deflatten_redirections(None, None, None) == []
    assert profile.plan_string_decrypt_calls(None, None, None, {}) == []


def test_default_profile_delegates_to_existing_resolvers(monkeypatch):
    profiles = import_module("plugins.DispatchThis.profiles")
    default = import_module("plugins.DispatchThis.profiles.default")

    monkeypatch.setattr(default, "resolve_llil_jump_plan", lambda *args: ("branch", args))
    monkeypatch.setattr(default, "plan_indirect_calls", lambda *args: ("call", args))
    monkeypatch.setattr(default, "_plan_global_constant_slots", lambda *args: ("global", args))
    monkeypatch.setattr(default, "compute_redirections", lambda *args, **kwargs: ("deflatten", args, kwargs))
    monkeypatch.setattr(default, "_plan_string_decrypt_calls", lambda *args: ("string", args))

    profile = profiles.get_profile("default")

    assert profile.resolve_branch_gadget("bv", "llil", {"known": "targets"}) == (
        "branch",
        ("bv", "llil", {"known": "targets"}),
    )
    assert profile.resolve_call_gadget("bv", "mlil") == ("call", ("bv", "mlil"))
    assert profile.plan_global_constant_slots("bv", "mlil") == ("global", ("bv", "mlil"))
    assert profile.plan_correlated_store_rewrites("bv", "func", "mlil") == []
    assert profile.plan_deflatten_redirections("bv", "func", "mlil") == (
        "deflatten",
        ("bv", "func"),
        {"mlil": "mlil"},
    )
    assert profile.plan_string_decrypt_calls("bv", "func", "mlil", {"stable": True}) == (
        "string",
        ("bv", "func", "mlil", {"stable": True}),
    )


def test_specialized_profiles_do_not_import_pass_planners():
    modules = (
        import_module("plugins.DispatchThis.profiles.dyzznb"),
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
        plan_correlated_store_rewrites=lambda *_args: [],
        plan_deflatten_redirections=lambda *_args: [],
        plan_string_decrypt_calls=lambda *_args: [],
    )
    configured = profiles.resolver_profile_from_module(module)
    monkeypatch.setitem(profiles._PROFILES, configured.id, configured)
    settings = FakeSettings({(profiles.ACTIVE_PROFILE_SETTING, "bv"): "configured"})

    profile = profiles.active_profile("bv", settings=settings)

    assert profile is configured


def test_active_profile_falls_back_to_default_and_warns(monkeypatch):
    profiles = import_module("plugins.DispatchThis.profiles")
    warnings = []
    settings = FakeSettings({(profiles.ACTIVE_PROFILE_SETTING, "bv"): "missing"})
    monkeypatch.setattr(profiles, "log_warn", warnings.append)

    profile = profiles.active_profile("bv", settings=settings)

    assert profile.id == "default"
    assert warnings == ["[profiles] unknown resolver profile 'missing'; using default"]


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
