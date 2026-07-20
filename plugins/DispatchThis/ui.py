"""Core-owned DispatchThis menus for provider binding and pass enablement."""

from __future__ import annotations

import binaryninja
from binaryninja import Settings, SettingsScope

from .passes.medium.branch_translate import clear_condition_failure_tags
from .providers import (
    ProviderBindingError,
    _pending_reproof_functions,
    _set_pending_reproof_functions,
    active_provider_id,
    get_provider,
    provider_ids,
    set_active_provider,
)
from .settings import (
    BRANCH_CONDITIONS_SETTING,
    BRANCH_TARGETS_SETTING,
    CALL_TARGETS_SETTING,
    DEFLATTEN_SETTING,
    GLOBAL_DATA_SETTING,
    PASS_LABELS,
    PASS_SETTING_IDS,
    dependents_for,
    prerequisites_for,
)
from .utils.log import log_info, log_warn
from .state import ROOT_KEY


SHORTCUTS = {
    "DispatchThis\\Toggle Indirect Branch Targets": "Alt+Q",
    "DispatchThis\\Toggle Deflatten": "Alt+W",
    "DispatchThis\\Toggle String Recovery": "Alt+E",
    "DispatchThis\\Disable All": "Alt+R",
}


def _settings(settings):
    return Settings() if settings is None else settings


def _reanalyze(func) -> None:
    try:
        func.reanalyze()
    except AttributeError:
        log_warn(f"[ui] {getattr(func, 'name', '<unknown>')}: reanalysis is unavailable")


def _clear_deflatten_stability(bv, func=None) -> None:
    stable = bv.session_data.get("dispatchthis_mlil_stable", {})
    if func is None:
        stable.clear()
    else:
        stable.pop(func.start, None)


def _invalidate_function_evidence(func) -> None:
    branch = func.session_data.get(ROOT_KEY, {}).get("branch", {})
    conditions = branch.get("conditions", {}) if type(branch) is dict else {}
    if type(conditions) is dict:
        clear_condition_failure_tags(func, conditions)
    func.session_data.pop(ROOT_KEY, None)


def _setting_label(key: str) -> str:
    return PASS_LABELS.get(key, key)


def set_function_pass(bv, func, key: str, enabled: bool, settings=None, reanalyze: bool = True) -> bool:
    """Apply one menu change while preserving the pass dependency closure."""

    if key not in PASS_SETTING_IDS:
        log_warn(f"[ui] {func.name}: unknown DispatchThis pass {key!r}")
        return False
    configured = _settings(settings)
    affected = prerequisites_for(key) if enabled else dependents_for(key)
    changed: list[str] = []
    for setting in affected:
        if configured.get_bool(setting, func) == enabled:
            continue
        if not configured.set_bool(setting, enabled, func, SettingsScope.SettingsResourceScope):
            log_warn(f"[ui] {func.name}: failed to update {_setting_label(setting)}")
            for updated in reversed(changed):
                configured.set_bool(updated, not enabled, func, SettingsScope.SettingsResourceScope)
            return False
        changed.append(setting)
    if not enabled and key in (
        BRANCH_TARGETS_SETTING,
        CALL_TARGETS_SETTING,
        GLOBAL_DATA_SETTING,
        BRANCH_CONDITIONS_SETTING,
    ):
        _invalidate_function_evidence(func)
    if key in (BRANCH_CONDITIONS_SETTING, DEFLATTEN_SETTING) or not enabled:
        _clear_deflatten_stability(bv, func)
    if reanalyze:
        _reanalyze(func)
    log_info(f"[ui] {func.name}: {'enabled' if enabled else 'disabled'} {_setting_label(key)}")
    return True


def toggle_function_pass(bv, func, key: str, settings=None) -> bool:
    """Toggle exactly one pass, expanding or shrinking its dependency closure."""

    configured = _settings(settings)
    return set_function_pass(bv, func, key, not configured.get_bool(key, func), configured)


def disable_function_settings(bv, func, settings=None, reanalyze: bool = True) -> bool:
    """Disable every visible DispatchThis pass for one function."""

    configured = _settings(settings)
    for key in PASS_SETTING_IDS:
        if not configured.set_bool(key, False, func, SettingsScope.SettingsResourceScope):
            log_warn(f"[ui] {func.name}: failed to disable {_setting_label(key)}")
            return False
    _invalidate_function_evidence(func)
    _clear_deflatten_stability(bv, func)
    if reanalyze:
        _reanalyze(func)
    log_info(f"[ui] {func.name}: disabled DispatchThis passes")
    return True


def use_provider(bv, func, provider_id: str, settings=None, reanalyze: bool = True) -> bool:
    """Bind a known provider to one view and discard stale function evidence."""

    configured = _settings(settings)
    try:
        current_provider_id = active_provider_id(bv, configured)
    except ProviderBindingError:
        current_provider_id = None
    binding_changed = current_provider_id != provider_id
    pending_before = _pending_reproof_functions(bv, configured)
    if binding_changed:
        if pending_before is None:
            log_warn(f"[ui] {func.name}: provider binding state is malformed")
            return False
        func_start = getattr(func, "start", None)
        if type(func_start) is not int or func_start < 0:
            log_warn(f"[ui] {func.name}: function start is invalid")
            return False
        affected = {
            candidate.start
            for candidate in getattr(bv, "functions", ())
            if type(getattr(candidate, "start", None)) is int and candidate.start >= 0
        }
        affected.add(func_start)
        if not _set_pending_reproof_functions(bv, pending_before | affected, configured):
            log_warn(f"[ui] {func.name}: failed to require provider branch reproof")
            return False
    if not set_active_provider(bv, provider_id, configured):
        if binding_changed:
            _set_pending_reproof_functions(bv, pending_before, configured)
        log_warn(f"[ui] {func.name}: unknown DispatchThis provider {provider_id!r}")
        return False
    if binding_changed:
        for candidate in getattr(bv, "functions", ()):
            _invalidate_function_evidence(candidate)
        _invalidate_function_evidence(func)
        _clear_deflatten_stability(bv)
    if reanalyze:
        _reanalyze(func)
    log_info(f"[ui] {func.name}: selected DispatchThis provider {provider_id}")
    return True


def select_provider(bv, func) -> bool:
    """Prompt for a provider using the registry as it exists at invocation time."""

    providers = tuple(
        (provider_id, provider)
        for provider_id in provider_ids()
        if (provider := get_provider(provider_id)) is not None
    )
    if not providers:
        log_warn(f"[ui] {func.name}: no DispatchThis providers are registered")
        return False
    try:
        from binaryninja.interaction import get_choice_input
    except ImportError:
        log_warn(f"[ui] {func.name}: provider selection UI is unavailable")
        return False
    choices = [f"{provider.name} ({provider_id})" for provider_id, provider in providers]
    choice = get_choice_input("Select Sample Semantics Provider", "DispatchThis", choices)
    if type(choice) is not int or choice < 0 or choice >= len(providers):
        return False
    return use_provider(bv, func, providers[choice][0])


def _valid_function(bv, func) -> bool:
    return bv is not None and func is not None


def _register_function_command(name: str, description: str, action) -> None:
    plugin_command = getattr(binaryninja, "PluginCommand", None)
    if plugin_command is not None:
        plugin_command.register_for_function(name, description, action, _valid_function)


def _register_shortcuts() -> None:
    try:
        from binaryninjaui import UIAction
        from PySide6.QtGui import QKeySequence
    except ImportError:
        return
    for name, shortcut in SHORTCUTS.items():
        UIAction.registerAction(f"Selection Target\\{name}", QKeySequence(shortcut))


def _schedule_shortcuts() -> None:
    try:
        from PySide6.QtCore import QTimer
    except ImportError:
        return
    execute_on_main_thread = getattr(binaryninja, "execute_on_main_thread", None)
    if execute_on_main_thread is None:
        return

    def schedule() -> None:
        for delay in (0, 250, 1000):
            QTimer.singleShot(delay, _register_shortcuts)

    execute_on_main_thread(schedule)


def register_ui_commands() -> None:
    """Register one provider selector and seven independent function pass menus."""

    _register_function_command(
        "DispatchThis\\Select Provider…",
        "Select the DispatchThis sample semantics provider for this BinaryView.",
        select_provider,
    )
    for key in PASS_SETTING_IDS:
        label = _setting_label(key)

        def action(bv, func, setting=key):
            return toggle_function_pass(bv, func, setting)

        _register_function_command(
            f"DispatchThis\\Toggle {label}",
            f"Toggle DispatchThis {label} for this function.",
            action,
        )
    _register_function_command(
        "DispatchThis\\Disable All",
        "Disable every DispatchThis pass for this function.",
        disable_function_settings,
    )
    _schedule_shortcuts()
