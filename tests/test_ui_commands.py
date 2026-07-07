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


def test_context_function_uses_plural_containing_function_lookup():
    func = FakeFunc()

    class BvWithContainingList(FakeBv):
        def get_function_at(self, _addr):
            return None

        def get_functions_containing(self, addr):
            return [func] if addr == 0x1001 else []

    ctx = type("Ctx", (), {"binaryView": BvWithContainingList(), "address": 0x1001})()

    assert ui._context_function(ctx) == (ctx.binaryView, func)


def test_ui_action_falls_back_to_active_ui_context(monkeypatch):
    bv = FakeBv()
    func = FakeFunc()
    action_context = type("ActionContext", (), {"binaryView": bv, "function": func})()
    empty_context = type("EmptyContext", (), {"binaryView": None, "function": None, "address": 0})()

    class FakeHandler:
        def actionContext(self):
            return action_context

    class FakeFrame:
        def actionHandler(self):
            return FakeHandler()

    class FakeUIContext:
        @staticmethod
        def activeContext():
            return FakeUIContext()

        def getCurrentViewFrame(self):
            return FakeFrame()

    binaryninjaui = types.ModuleType("binaryninjaui")
    binaryninjaui.UIContext = FakeUIContext
    monkeypatch.setitem(sys.modules, "binaryninjaui", binaryninjaui)

    calls = []
    ui._ui_action(lambda got_bv, got_func: calls.append((got_bv, got_func)))(empty_context)

    assert calls == [(bv, func)]


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


def test_register_shortcuts_sets_key_for_pre_registered_plugin_command(monkeypatch):
    class FakeKeySequence:
        def __init__(self, text):
            self.text = text

    class FakeUIAction:
        registered = {"DispatchThis\\Toggle Resolver": None}

        def __init__(self, action):
            self.action = action

        @classmethod
        def registerAction(cls, name, key_sequence):
            cls.registered[name] = key_sequence.text

        @classmethod
        def getKeyBinding(cls, name):
            return [cls.registered[name]] if cls.registered.get(name) else []

    class FakeUIActionHandler:
        bound = []

        @classmethod
        def globalActions(cls):
            return cls()

        def bindAction(self, name, action):
            self.bound.append((name, action))

    binaryninjaui = types.ModuleType("binaryninjaui")
    binaryninjaui.UIAction = FakeUIAction
    binaryninjaui.UIActionHandler = FakeUIActionHandler
    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QKeySequence = FakeKeySequence

    monkeypatch.setitem(sys.modules, "binaryninjaui", binaryninjaui)
    monkeypatch.setitem(sys.modules, "PySide6", types.ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)

    assert ui._register_shortcuts({"DispatchThis\\Toggle Resolver": lambda *_args: None})
    assert FakeUIAction.registered["DispatchThis\\Toggle Resolver"] == "Ctrl+Alt+R"
    assert [name for name, _action in FakeUIActionHandler.bound] == ["DispatchThis\\Toggle Resolver"]


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

    def register_shortcuts(actions):
        calls.append(actions)
        return len(calls) > 1

    monkeypatch.setattr(ui, "_register_shortcuts", register_shortcuts)

    ui.register_ui_commands("resolve", "deflatten", "string")

    assert len(calls) == 1
    assert len(scheduled) == 1
    scheduled[0]()
    assert len(calls) == 2
