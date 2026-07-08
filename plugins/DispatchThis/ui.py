import binaryninja
from binaryninja import Settings, SettingsScope

from .profiles import profile_ids, set_active_profile
from .utils.log import log_info, log_warn


SHORTCUTS = {
    "DispatchThis\\Toggle Resolver": "Alt+Q",
    "DispatchThis\\Toggle Deflatten": "Alt+W",
    "DispatchThis\\Toggle String Decrypt": "Alt+E",
    "DispatchThis\\Disable All": "Alt+R",
}


def _settings(settings):
    return settings or Settings()


def _reanalyze(bv, func):
    try:
        func.reanalyze()
        bv.update_analysis_and_wait()
    except Exception as exc:  # noqa: BLE001
        log_warn(f"[ui] {getattr(func, 'name', '<unknown>')}: reanalysis failed: {exc}")


def _setting_label(key):
    return {
        "analysis.plugins.dispatchThis.indirectJumpsCalls": "resolver",
        "analysis.plugins.dispatchThis.deflatten": "deflatten",
        "analysis.plugins.dispatchThis.stringDecrypt": "string decrypt",
    }[key]


def toggle_function_setting(bv, func, key, settings=None, reanalyze=True):
    settings = _settings(settings)
    enabled = not settings.get_bool(key, func)
    settings.set_bool(key, enabled, func, SettingsScope.SettingsResourceScope)
    if reanalyze:
        _reanalyze(bv, func)
    state = "enabled" if enabled else "disabled"
    log_info(f"[ui] {func.name}: {state} {_setting_label(key)}")
    return enabled


def disable_function_settings(bv, func, keys, settings=None, reanalyze=True):
    settings = _settings(settings)
    for key in keys:
        settings.set_bool(key, False, func, SettingsScope.SettingsResourceScope)
    if reanalyze:
        _reanalyze(bv, func)
    log_info(f"[ui] {func.name}: disabled DispatchThis function settings")


def use_profile(bv, func, profile_id, reanalyze=True):
    set_active_profile(bv, profile_id)
    if reanalyze:
        _reanalyze(bv, func)
    log_info(f"[ui] {func.name}: selected DispatchThis profile {profile_id}")


def _valid_function(bv, func):
    return bv is not None and func is not None


def _register_function_command(name, description, action):
    plugin_command = getattr(binaryninja, "PluginCommand", None)
    if plugin_command is None:
        return
    plugin_command.register_for_function(name, description, action, _valid_function)


def _register_shortcuts():
    try:
        from binaryninjaui import UIAction
        from PySide6.QtGui import QKeySequence
    except Exception:  # noqa: BLE001
        return

    for name, shortcut in SHORTCUTS.items():
        shortcut_action = f"Selection Target\\{name}"
        try:
            UIAction.registerAction(shortcut_action, QKeySequence(shortcut))
        except Exception as exc:  # noqa: BLE001
            log_warn(f"[ui] failed to register shortcut for {name}: {exc}")


def _schedule_shortcuts():
    try:
        from PySide6.QtCore import QTimer
    except Exception:  # noqa: BLE001
        return
    execute_on_main_thread = getattr(binaryninja, "execute_on_main_thread", None)
    if execute_on_main_thread is None:
        return

    def schedule():
        for delay in (0, 250, 1000):
            QTimer.singleShot(delay, _register_shortcuts)

    try:
        execute_on_main_thread(schedule)
    except Exception as exc:  # noqa: BLE001
        log_warn(f"[ui] failed to schedule shortcut registration: {exc}")


def register_ui_commands(resolve_key, deflatten_key, string_decrypt_key):
    setting_actions = {
        "DispatchThis\\Toggle Resolver": (
            "Toggle DispatchThis resolver for this function.",
            lambda bv, func: toggle_function_setting(bv, func, resolve_key),
        ),
        "DispatchThis\\Toggle Deflatten": (
            "Toggle DispatchThis deflattening for this function.",
            lambda bv, func: toggle_function_setting(bv, func, deflatten_key),
        ),
        "DispatchThis\\Toggle String Decrypt": (
            "Toggle DispatchThis string decrypt for this function.",
            lambda bv, func: toggle_function_setting(bv, func, string_decrypt_key),
        ),
        "DispatchThis\\Disable All": (
            "Disable DispatchThis function settings for this function.",
            lambda bv, func: disable_function_settings(
                bv,
                func,
                (resolve_key, deflatten_key, string_decrypt_key),
            ),
        ),
    }

    for profile_id in profile_ids():
        name = f"DispatchThis\\Profile\\Use {profile_id}"
        action = lambda bv, func, profile_id=profile_id: use_profile(bv, func, profile_id)
        _register_function_command(
            name,
            f"Select the DispatchThis {profile_id} resolver profile for this view.",
            action,
        )

    for name, (description, action) in setting_actions.items():
        _register_function_command(name, description, action)

    _schedule_shortcuts()
