from collections import namedtuple
import json

from binaryninja import Settings, SettingsScope

from . import default, driver_2_6, dyzznb, valorant_2_6
from ..utils.log import log_warn


DEFAULT_PROFILE_ID = "default"
ACTIVE_PROFILE_SETTING = "analysis.plugins.dispatchThis.resolverProfile"
REQUIRED_HOOKS = (
    "resolve_branch_gadget",
    "resolve_call_gadget",
    "plan_global_constant_slots",
    "plan_correlated_store_rewrites",
    "plan_deflatten_redirections",
    "plan_string_decrypt_calls",
)

ResolverProfile = namedtuple(
    "ResolverProfile",
    ("id", "name", "description", *REQUIRED_HOOKS),
)


class InvalidResolverProfile(ValueError):
    pass


def resolver_profile_from_module(module):
    missing = [name for name in REQUIRED_HOOKS if not callable(getattr(module, name, None))]
    if missing:
        raise InvalidResolverProfile(
            f"resolver profile {getattr(module, 'PROFILE_ID', '<unknown>')} "
            f"missing hook(s): {', '.join(missing)}"
        )
    return ResolverProfile(
        module.PROFILE_ID,
        module.PROFILE_NAME,
        module.PROFILE_DESCRIPTION,
        *(getattr(module, name) for name in REQUIRED_HOOKS),
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
    log_warn(f"[profiles] unknown resolver profile {profile_id!r}; using default")
    return DEFAULT_PROFILE_ID


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
