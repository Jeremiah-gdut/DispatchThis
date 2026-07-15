from collections import namedtuple
import json

from binaryninja import Settings, SettingsScope

from . import default, driver_2_6, dyzznb, valorant_2_6
from ..utils.log import log_warn


DEFAULT_PROFILE_ID = "default"
ACTIVE_PROFILE_SETTING = "analysis.plugins.dispatchThis.resolverProfile"
PROFILE_HOOKS = (
    "resolve_branch_gadget",
    "resolve_call_gadget",
    "plan_global_constant_slots",
    "plan_deflatten_redirections",
)

ResolverProfile = namedtuple(
    "ResolverProfile",
    ("id", "name", "description", *PROFILE_HOOKS, "correlated_stores"),
)


class InvalidResolverProfile(ValueError):
    pass


_MISSING = object()


def _no_recovery(*_args, **_kwargs):
    return []


def resolver_profile_from_module(module):
    hooks = []
    invalid = []
    for name in PROFILE_HOOKS:
        hook = getattr(module, name, _MISSING)
        if hook is _MISSING:
            hook = _no_recovery
        elif not callable(hook):
            invalid.append(name)
        hooks.append(hook)
    correlated_stores = getattr(module, "correlated_stores", None)
    if correlated_stores is not None and not callable(correlated_stores):
        invalid.append("correlated_stores")
    if invalid:
        raise InvalidResolverProfile(
            f"resolver profile {getattr(module, 'PROFILE_ID', '<unknown>')} "
            f"invalid hook(s): {', '.join(invalid)}"
        )
    return ResolverProfile(
        module.PROFILE_ID,
        module.PROFILE_NAME,
        module.PROFILE_DESCRIPTION,
        *hooks,
        correlated_stores,
    )


_PROFILES = {
    DEFAULT_PROFILE_ID: resolver_profile_from_module(default),
    driver_2_6.PROFILE_ID: resolver_profile_from_module(driver_2_6),
    dyzznb.PROFILE_ID: resolver_profile_from_module(dyzznb),
    valorant_2_6.PROFILE_ID: resolver_profile_from_module(valorant_2_6),
}


def get_profile(profile_id):
    return _PROFILES[profile_id]


def profile_ids():
    return tuple(sorted(_PROFILES))


def register_profile_settings(settings=None):
    settings = settings or Settings()
    settings.register_group("analysis.plugins.dispatchThis", "DispatchThis")
    return settings.register_setting(ACTIVE_PROFILE_SETTING, json.dumps({
        "title": "Resolver Profile",
        "description": "Active DispatchThis resolver profile for this BinaryView.",
        "type": "string",
        "default": DEFAULT_PROFILE_ID,
        "enum": list(profile_ids()),
    }))


def _settings(settings):
    return settings or Settings()


def active_profile_id(bv, settings=None):
    profile_id = _settings(settings).get_string(ACTIVE_PROFILE_SETTING, bv) or DEFAULT_PROFILE_ID
    if profile_id in _PROFILES:
        return profile_id
    log_warn(f"[profiles] unknown resolver profile {profile_id!r}; refusing resolver work")
    raise InvalidResolverProfile(f"unknown resolver profile {profile_id!r}")


def active_profile(bv, settings=None):
    return get_profile(active_profile_id(bv, settings))


def set_active_profile(bv, profile_id, settings=None):
    get_profile(profile_id)
    return _settings(settings).set_string(
        ACTIVE_PROFILE_SETTING,
        profile_id,
        bv,
        SettingsScope.SettingsResourceScope,
    )
