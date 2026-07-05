from collections import namedtuple

from . import default


REQUIRED_HOOKS = (
    "resolve_branch_gadget",
    "resolve_call_gadget",
    "plan_global_constant_slots",
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
    "default": resolver_profile_from_module(default),
}


def get_profile(profile_id):
    return _PROFILES[profile_id]


def profile_ids():
    return tuple(sorted(_PROFILES))
