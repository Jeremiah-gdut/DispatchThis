"""Core-owned registry and BinaryView binding for sample semantics providers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from binaryninja import Settings, SettingsScope

from .semantics import (
    CORE_API_VERSION,
    SLOT_NAMES,
    BranchTargetFact,
    BranchTargetQuery,
    CompleteBatch,
    Inconclusive,
    ProviderContractError,
    SampleSemantics,
)
from .utils.log import log_warn


ACTIVE_PROVIDER_SETTING = "analysis.plugins.dispatchThis.provider"
_PROVIDER_REPROOF_PENDING_FUNCTIONS_SETTING = "analysis.plugins.dispatchThis.providerReproofPendingFunctions"

_PROVIDERS: dict[str, SampleSemantics] = {}
_REJECTION_LOGS: set[tuple[str, str]] = set()
_LEGACY_PROFILES: dict[str, "_LegacyProfile"] = {}


class _LegacyProfile(Protocol):
    id: str
    name: str
    description: str

    def resolve_branch_gadget(self, bv, llil, known_targets): ...


class ProviderBindingError(RuntimeError):
    """The current BinaryView has no usable DispatchThis provider binding."""


def _warn_once(kind: str, provider_id: str, message: str) -> None:
    key = (kind, provider_id)
    if key not in _REJECTION_LOGS:
        _REJECTION_LOGS.add(key)
        log_warn(message)


def _registration_error(provider: SampleSemantics) -> str | None:
    if type(provider) is not SampleSemantics:
        return "provider must be an exact SampleSemantics instance"
    if provider.api_version != CORE_API_VERSION:
        return (
            f"provider API version {provider.api_version} does not match "
            f"core API version {CORE_API_VERSION}"
        )
    for name in SLOT_NAMES:
        slot = getattr(provider, name)
        if slot is not None and not callable(slot):
            return f"provider slot {name} is not callable"
    return None


def register_provider(provider: SampleSemantics) -> bool:
    """Register one exact-version provider without replacing an existing ID."""

    if type(provider) is not SampleSemantics:
        _warn_once("invalid", "<unknown>", "[providers] rejected provider: invalid contract object")
        return False
    error = _registration_error(provider)
    if error is not None:
        _warn_once("invalid", provider.provider_id, f"[providers] rejected {provider.provider_id!r}: {error}")
        return False
    if provider.provider_id in _PROVIDERS:
        _warn_once(
            "duplicate",
            provider.provider_id,
            f"[providers] rejected duplicate provider ID {provider.provider_id!r}",
        )
        return False
    _PROVIDERS[provider.provider_id] = provider
    return True


def get_provider(provider_id: str) -> SampleSemantics | None:
    """Return a registered provider by stable ID without choosing a fallback."""

    return _PROVIDERS.get(provider_id) if type(provider_id) is str else None


def provider_ids() -> tuple[str, ...]:
    """Return the currently registered stable provider IDs."""

    return tuple(sorted(_PROVIDERS))


def register_provider_settings(settings: Settings | None = None) -> bool:
    """Register the persistent BinaryView-level provider binding setting."""

    configured = Settings() if settings is None else settings
    configured.register_group("analysis.plugins.dispatchThis", "DispatchThis")
    provider_registered = configured.register_setting(
        ACTIVE_PROVIDER_SETTING,
        json.dumps(
            {
                "title": "Sample Semantics Provider",
                "description": "Explicit DispatchThis provider ID for this BinaryView.",
                "type": "string",
                "default": "",
            }
        ),
    )
    reproof_registered = configured.register_setting(
        _PROVIDER_REPROOF_PENDING_FUNCTIONS_SETTING,
        json.dumps(
            {
                "title": "Internal Provider Reproof State",
                "description": "Internal DispatchThis state; do not edit manually.",
                "type": "string",
                "default": "[]",
            }
        ),
    )
    return provider_registered and reproof_registered


def _settings(settings: Settings | None) -> Settings:
    return Settings() if settings is None else settings


def active_provider_id(bv, settings: Settings | None = None) -> str:
    """Read the selected provider ID and reject a missing binding."""

    provider_id = _settings(settings).get_string(ACTIVE_PROVIDER_SETTING, bv)
    if type(provider_id) is not str or not provider_id:
        raise ProviderBindingError("no DispatchThis provider is selected for this BinaryView")
    return provider_id


def active_provider(bv, settings: Settings | None = None) -> SampleSemantics:
    """Resolve the explicit BinaryView binding without any default provider."""

    provider_id = active_provider_id(bv, settings)
    provider = get_provider(provider_id)
    if provider is None:
        raise ProviderBindingError(f"unknown DispatchThis provider {provider_id!r}")
    return provider


def set_active_provider(bv, provider_id: str, settings: Settings | None = None) -> bool:
    """Persist a known provider ID on one BinaryView resource."""

    if get_provider(provider_id) is None:
        return False
    return _settings(settings).set_string(
        ACTIVE_PROVIDER_SETTING,
        provider_id,
        bv,
        SettingsScope.SettingsResourceScope,
    )


def _pending_reproof_functions(bv, settings: Settings | None = None) -> frozenset[int] | None:
    """Read the persistent per-function reproof guard, failing closed if malformed."""

    raw = _settings(settings).get_string(_PROVIDER_REPROOF_PENDING_FUNCTIONS_SETTING, bv)
    try:
        starts = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if type(starts) is not list or any(type(start) is not int or start < 0 for start in starts):
        return None
    return frozenset(starts)


def _set_pending_reproof_functions(bv, starts, settings: Settings | None = None) -> bool:
    """Persist exact function starts that need the newly selected provider's proof."""

    try:
        normalized = tuple(starts)
    except TypeError:
        return False
    if any(type(start) is not int or start < 0 for start in normalized):
        return False
    normalized = tuple(sorted(set(normalized)))
    return _settings(settings).set_string(
        _PROVIDER_REPROOF_PENDING_FUNCTIONS_SETTING,
        json.dumps(normalized, separators=(",", ":")),
        bv,
        SettingsScope.SettingsResourceScope,
    )


def _legacy_branch_targets(profile: _LegacyProfile, query: BranchTargetQuery) -> CompleteBatch[BranchTargetFact] | Inconclusive:
    """Translate the old profile planner while it is still being migrated."""
    try:
        raw_plans = profile.resolve_branch_gadget(query.view, query.llil, {})
    except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — legacy plugin boundary; never let it mutate core state.
        return Inconclusive(f"legacy branch planner failed: {error}")
    if not isinstance(raw_plans, tuple | list):
        return Inconclusive("legacy branch planner returned a non-sequence")

    facts: list[BranchTargetFact] = []
    try:
        for raw_plan in raw_plans:
            if not isinstance(raw_plan, Mapping):
                return Inconclusive("legacy branch planner returned a malformed plan")
            jump_il = raw_plan.get("jump_il")
            raw_targets = raw_plan.get("targets")
            if not isinstance(raw_targets, (tuple, list, set, frozenset)):
                return Inconclusive("legacy branch planner returned malformed targets")
            targets = tuple(sorted(set(raw_targets)))
            facts.append(BranchTargetFact(jump_il, targets))
    except (ProviderContractError, TypeError):
        return Inconclusive("legacy branch planner returned an invalid branch fact")
    return CompleteBatch(tuple(facts))


def _register_legacy_profile(profile: _LegacyProfile) -> bool:
    """Install one bundled profile behind the private migration adapter."""

    provider = SampleSemantics(
        provider_id=profile.id,
        name=profile.name,
        api_version=CORE_API_VERSION,
        branch_targets=lambda query: _legacy_branch_targets(profile, query),
    )
    if not register_provider(provider):
        return False
    _LEGACY_PROFILES[profile.id] = profile
    return True


def _legacy_profile(provider_id: str) -> _LegacyProfile | None:
    """Return a private migration adapter backing object, if selected."""

    return _LEGACY_PROFILES.get(provider_id)


__all__ = (
    "ACTIVE_PROVIDER_SETTING",
    "ProviderBindingError",
    "active_provider",
    "active_provider_id",
    "get_provider",
    "provider_ids",
    "register_provider",
    "register_provider_settings",
    "set_active_provider",
)
