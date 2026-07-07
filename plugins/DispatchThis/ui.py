import binaryninja
from binaryninja import Settings, SettingsScope

from .profiles import profile_ids, set_active_profile
from .utils.log import log_info, log_warn


SHORTCUTS = {
    "DispatchThis\\Toggle Resolver": "Ctrl+Alt+R",
    "DispatchThis\\Toggle Deflatten": "Ctrl+Alt+D",
    "DispatchThis\\Toggle String Decrypt": "Ctrl+Alt+S",
    "DispatchThis\\Disable All": "Ctrl+Alt+X",
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
        return False
    plugin_command.register_for_function(name, description, action, _valid_function)
    return True


def _context_function_from(ctx):
    bv = getattr(ctx, "binaryView", None)
    func = getattr(ctx, "function", None)
    if func is None and bv is not None:
        addr = getattr(ctx, "address", None)
        if addr is not None:
            func = bv.get_function_at(addr)
            if func is None and hasattr(bv, "get_function_containing"):
                func = bv.get_function_containing(addr)
            if func is None and hasattr(bv, "get_functions_containing"):
                funcs = bv.get_functions_containing(addr)
                func = funcs[0] if funcs else None
    return bv, func


def _current_ui_context_function():
    try:
        from binaryninjaui import UIContext
    except Exception:  # noqa: BLE001
        return None, None
    try:
        context = UIContext.activeContext()
        frame = context.getCurrentViewFrame() if context is not None else None
        handler = frame.actionHandler() if frame is not None else None
        action_context = handler.actionContext() if handler is not None else None
    except Exception as exc:  # noqa: BLE001
        log_warn(f"[ui] failed to read active UI context: {exc}")
        return None, None
    return _context_function_from(action_context)


def _context_function(ctx):
    bv, func = _context_function_from(ctx)
    if _valid_function(bv, func):
        return bv, func
    return _current_ui_context_function()


def _ui_action(action):
    def wrapped(ctx):
        bv, func = _context_function(ctx)
        if not _valid_function(bv, func):
            log_warn("[ui] DispatchThis shortcut requires an active function")
            return
        action(bv, func)

    return wrapped


def _register_shortcuts(actions):
    try:
        from binaryninjaui import UIAction, UIActionHandler
        from PySide6.QtGui import QKeySequence
    except Exception:  # noqa: BLE001
        return False

    handler = UIActionHandler.globalActions()
    registered = True
    for name, action in actions.items():
        shortcut = SHORTCUTS.get(name)
        if shortcut is None:
            continue
        try:
            UIAction.registerAction(name, QKeySequence(shortcut))
            handler.bindAction(name, UIAction(_ui_action(action)))
            registered = bool(UIAction.getKeyBinding(name)) and registered
        except Exception as exc:  # noqa: BLE001
            log_warn(f"[ui] failed to register shortcut for {name}: {exc}")
            registered = False
    return registered


def _retry_shortcuts_on_main_thread(actions):
    execute_on_main_thread = getattr(binaryninja, "execute_on_main_thread", None)
    if execute_on_main_thread is None:
        return False
    try:
        execute_on_main_thread(lambda: _register_shortcuts(actions))
        return True
    except Exception as exc:  # noqa: BLE001
        log_warn(f"[ui] failed to schedule shortcut registration retry: {exc}")
        return False


def _retry_shortcuts_when_ui_ready(actions):
    try:
        import binaryninjaui  # noqa: F401
        from PySide6.QtCore import QTimer
    except Exception:  # noqa: BLE001
        return False
    try:
        for delay in (250, 1000, 3000):
            QTimer.singleShot(delay, lambda actions=actions: _register_shortcuts(actions))
        return True
    except Exception as exc:  # noqa: BLE001
        log_warn(f"[ui] failed to schedule delayed shortcut registration: {exc}")
        return False


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

    actions = {}
    for profile_id in profile_ids():
        name = f"DispatchThis\\Profile\\Use {profile_id}"
        action = lambda bv, func, profile_id=profile_id: use_profile(bv, func, profile_id)
        _register_function_command(
            name,
            f"Select the DispatchThis {profile_id} resolver profile for this view.",
            action,
        )
        actions[name] = action

    for name, (description, action) in setting_actions.items():
        _register_function_command(name, description, action)
        actions[name] = action

    if not _register_shortcuts(actions):
        _retry_shortcuts_on_main_thread(actions)
        _retry_shortcuts_when_ui_ready(actions)
