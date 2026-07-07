import binaryninja
import sys
import types

from conftest import load_plugin_module


ui = load_plugin_module("plugins.DispatchThis.ui")


class FakeSettings:
    def __init__(self):
        self.bools = {}
        self.writes = []

    def get_bool(self, key, resource=None):
        return self.bools.get((key, resource), False)

    def set_bool(self, key, value, resource=None, scope=None):
        self.bools[(key, resource)] = value
        self.writes.append((key, value, resource, scope))
        return True


class FakeBv:
    def __init__(self):
        self.updated = 0

    def update_analysis_and_wait(self):
        self.updated += 1


class FakeFunc:
    name = "sub_1000"

    def __init__(self):
        self.reanalyzed = 0

    def reanalyze(self):
        self.reanalyzed += 1


class FakePluginCommand:
    registered = []

    @classmethod
    def register_for_function(cls, name, description, action, is_valid=None):
        cls.registered.append((name, description, action, is_valid))


def test_toggle_function_setting_flips_current_function_resource_setting():
    bv = FakeBv()
    func = FakeFunc()
    settings = FakeSettings()

    assert ui.toggle_function_setting(bv, func, "analysis.plugins.dispatchThis.indirectJumpsCalls", settings) is True
    assert ui.toggle_function_setting(bv, func, "analysis.plugins.dispatchThis.indirectJumpsCalls", settings) is False

    assert settings.writes == [
        ("analysis.plugins.dispatchThis.indirectJumpsCalls", True, func, ui.SettingsScope.SettingsResourceScope),
        ("analysis.plugins.dispatchThis.indirectJumpsCalls", False, func, ui.SettingsScope.SettingsResourceScope),
    ]
    assert func.reanalyzed == 2
    assert bv.updated == 2


def test_disable_function_settings_clears_only_the_requested_keys():
    bv = FakeBv()
    func = FakeFunc()
    settings = FakeSettings()
    keys = ("resolve", "deflatten", "string")

    ui.disable_function_settings(bv, func, keys, settings)

    assert settings.writes == [
        ("resolve", False, func, ui.SettingsScope.SettingsResourceScope),
        ("deflatten", False, func, ui.SettingsScope.SettingsResourceScope),
        ("string", False, func, ui.SettingsScope.SettingsResourceScope),
    ]
    assert func.reanalyzed == 1
    assert bv.updated == 1


def test_use_profile_updates_view_profile_without_function_settings(monkeypatch):
    bv = FakeBv()
    func = FakeFunc()
    calls = []
    monkeypatch.setattr(ui, "set_active_profile", lambda bv_arg, profile_id: calls.append((bv_arg, profile_id)))

    ui.use_profile(bv, func, "dyzznb")

    assert calls == [(bv, "dyzznb")]
    assert func.reanalyzed == 1
    assert bv.updated == 1


def test_register_ui_commands_adds_profile_and_toggle_function_commands(monkeypatch):
    FakePluginCommand.registered = []
    monkeypatch.setattr(binaryninja, "PluginCommand", FakePluginCommand, raising=False)
    monkeypatch.setattr(ui, "_register_shortcuts", lambda _actions: False)

    ui.register_ui_commands("resolve", "deflatten", "string")

    names = [item[0] for item in FakePluginCommand.registered]
    assert "DispatchThis\\Profile\\Use default" in names
    assert "DispatchThis\\Profile\\Use dyzznb" in names
    assert "DispatchThis\\Toggle Resolver" in names
    assert "DispatchThis\\Toggle Deflatten" in names
    assert "DispatchThis\\Toggle String Decrypt" in names
    assert "DispatchThis\\Disable All" in names


def test_register_shortcuts_sets_key_on_selection_target_action(monkeypatch):
    class FakeKeySequence:
        def __init__(self, text):
            self.text = text

    class FakeUIAction:
        registered = {"Selection Target\\DispatchThis\\Toggle Resolver": None}
        unregistered = []

        @classmethod
        def registerAction(cls, name, key_sequence):
            cls.registered[name] = key_sequence.text

        @classmethod
        def isActionRegistered(cls, name):
            return name == "DispatchThis\\Shortcuts\\Toggle Resolver"

        @classmethod
        def unregisterAction(cls, name):
            cls.unregistered.append(name)

        @classmethod
        def getKeyBinding(cls, name):
            return [cls.registered[name]] if cls.registered.get(name) else []

    binaryninjaui = types.ModuleType("binaryninjaui")
    binaryninjaui.UIAction = FakeUIAction
    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QKeySequence = FakeKeySequence

    monkeypatch.setitem(sys.modules, "binaryninjaui", binaryninjaui)
    monkeypatch.setitem(sys.modules, "PySide6", types.ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)

    assert ui._register_shortcuts({"DispatchThis\\Toggle Resolver": lambda *_args: None})
    assert FakeUIAction.registered["Selection Target\\DispatchThis\\Toggle Resolver"] == "Ctrl+Alt+J"
    assert FakeUIAction.unregistered == ["DispatchThis\\Shortcuts\\Toggle Resolver"]


def test_register_ui_commands_retries_shortcuts_on_main_thread(monkeypatch):
    FakePluginCommand.registered = []
    scheduled = []
    calls = []

    monkeypatch.setattr(binaryninja, "PluginCommand", FakePluginCommand, raising=False)
    monkeypatch.setattr(
        binaryninja,
        "execute_on_main_thread",
        lambda callback: scheduled.append(callback),
        raising=False,
    )
    monkeypatch.setattr(ui, "_retry_shortcuts_when_ui_ready", lambda _actions: False)

    def register_shortcuts(actions):
        calls.append(actions)
        return len(calls) > 1

    monkeypatch.setattr(ui, "_register_shortcuts", register_shortcuts)

    ui.register_ui_commands("resolve", "deflatten", "string")

    assert calls == []
    assert len(scheduled) == 1
    scheduled[0]()
    assert len(calls) == 1


def test_register_ui_commands_retries_shortcuts_when_ui_ready(monkeypatch):
    FakePluginCommand.registered = []
    main_thread = []
    scheduled = []
    calls = []

    monkeypatch.setattr(binaryninja, "PluginCommand", FakePluginCommand, raising=False)
    monkeypatch.setattr(ui, "_retry_shortcuts_on_main_thread", lambda _actions: False)
    monkeypatch.setattr(binaryninja, "execute_on_main_thread", lambda callback: main_thread.append(callback), raising=False)

    def register_shortcuts(actions):
        calls.append(actions)
        return False

    class FakeTimer:
        @staticmethod
        def singleShot(delay, callback):
            scheduled.append((delay, callback))

    binaryninjaui = types.ModuleType("binaryninjaui")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QTimer = FakeTimer

    monkeypatch.setattr(ui, "_register_shortcuts", register_shortcuts)
    monkeypatch.setitem(sys.modules, "binaryninjaui", binaryninjaui)
    monkeypatch.setitem(sys.modules, "PySide6", types.ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)

    ui.register_ui_commands("resolve", "deflatten", "string")

    assert calls == []
    assert len(main_thread) == 1
    assert scheduled == []
    main_thread[0]()
    assert [delay for delay, _callback in scheduled] == [250, 1000, 3000]
    scheduled[0][1]()
    assert len(calls) == 1


def test_register_ui_commands_schedules_delayed_shortcuts(monkeypatch):
    FakePluginCommand.registered = []
    delayed = []

    monkeypatch.setattr(binaryninja, "PluginCommand", FakePluginCommand, raising=False)
    monkeypatch.setattr(ui, "_register_shortcuts", lambda _actions: True)
    monkeypatch.setattr(ui, "_retry_shortcuts_when_ui_ready", lambda actions: delayed.append(actions))

    ui.register_ui_commands("resolve", "deflatten", "string")

    assert len(delayed) == 1
