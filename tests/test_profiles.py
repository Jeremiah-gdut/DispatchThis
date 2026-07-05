import types
from importlib import import_module

import pytest


def test_default_resolver_profile_is_registered():
    profiles = import_module("plugins.DispatchThis.profiles")

    profile = profiles.get_profile("default")

    assert profile.id == "default"
    assert profile.name
    assert profile.description
    assert profile.resolve_branch_gadget is not None
    assert profile.resolve_call_gadget is not None
    assert profile.plan_global_constant_slots is not None


def test_profile_without_required_hook_is_rejected():
    profiles = import_module("plugins.DispatchThis.profiles")
    module = types.SimpleNamespace(
        PROFILE_ID="broken",
        PROFILE_NAME="Broken",
        PROFILE_DESCRIPTION="Missing call hook",
        resolve_branch_gadget=lambda *_args: [],
        plan_global_constant_slots=lambda *_args: [],
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
    )

    profile = profiles.resolver_profile_from_module(module)

    assert profile.id == "noop"
    assert profile.resolve_branch_gadget(None, None, None) == []
    assert profile.resolve_call_gadget(None, None, None) == []
    assert profile.plan_global_constant_slots(None, None) == []


def test_default_profile_delegates_to_existing_resolvers(monkeypatch):
    profiles = import_module("plugins.DispatchThis.profiles")
    default = import_module("plugins.DispatchThis.profiles.default")

    monkeypatch.setattr(default, "resolve_llil_jump_plan", lambda *args: ("branch", args))
    monkeypatch.setattr(default, "plan_indirect_calls", lambda *args: ("call", args))
    monkeypatch.setattr(default, "_plan_global_constant_slots", lambda *args: ("global", args))

    profile = profiles.get_profile("default")

    assert profile.resolve_branch_gadget("bv", "llil", {"known": "targets"}) == (
        "branch",
        ("bv", "llil", {"known": "targets"}),
    )
    assert profile.resolve_call_gadget("bv", "mlil") == ("call", ("bv", "mlil"))
    assert profile.plan_global_constant_slots("bv", "mlil") == ("global", ("bv", "mlil"))
