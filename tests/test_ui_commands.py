import binaryninja
import sys
import types

from conftest import load_plugin_module


ui = load_plugin_module("plugins.DispatchThis.ui")
settings_module = load_plugin_module("plugins.DispatchThis.settings")


class FakeSettings:
    def __init__(self):
        self.bools = {}
        self.integers = {}
        self.writes = []

    def get_bool(self, key, resource=None):
        return self.bools.get((key, resource), False)

    def set_bool(self, key, value, resource=None, scope=None):
        self.bools[(key, resource)] = value
        self.writes.append((key, value, resource, scope))
        return True

    def get_integer(self, key, resource=None):
        return self.integers.get((key, resource), -1)

    def set_integer(self, key, value, resource=None, scope=None):
        self.integers[(key, resource)] = value
        return True


class FakeAnalysisParameters:
    def __init__(self):
        self.maxFunctionSize = 0x10000
        self.maxFunctionAnalysisTime = 600000
        self.maxFunctionUpdateCount = 100


class FakeBv:
    def __init__(self, events=None):
        self.session_data = {}
        self.functions = []
        self.events = [] if events is None else events
        self._parameters_for_analysis = FakeAnalysisParameters()

    @property
    def parameters_for_analysis(self):
        return self._parameters_for_analysis

    @parameters_for_analysis.setter
    def parameters_for_analysis(self, parameters):
        self.events.append("parameters")
        self._parameters_for_analysis = parameters


class FakeFunc:
    name = "sub_1000"
    start = 0x1000

    def __init__(self, events=None):
        self.reanalyzed = 0
        self.session_data = {}
        self.removed_tags = []
        self.events = [] if events is None else events

    def reanalyze(self):
        self.reanalyzed += 1
        self.events.append("reanalyze")

    def remove_auto_address_tags_of_type(self, source, tag_type):
        self.removed_tags.append((source, tag_type))


class FakePluginCommand:
    registered = []

    @classmethod
    def register_for_function(cls, name, description, action, is_valid=None):
        cls.registered.append((name, description, action, is_valid))


def test_toggle_function_pass_flips_current_function_resource_setting():
    bv = FakeBv()
    func = FakeFunc()
    settings = FakeSettings()

    assert ui.toggle_function_pass(bv, func, settings_module.BRANCH_TARGETS_SETTING, settings) is True
    assert ui.toggle_function_pass(bv, func, settings_module.BRANCH_TARGETS_SETTING, settings) is True

    assert settings.writes == [
        (settings_module.BRANCH_TARGETS_SETTING, True, func, ui.SettingsScope.SettingsResourceScope),
        (settings_module.BRANCH_TARGETS_SETTING, False, func, ui.SettingsScope.SettingsResourceScope),
    ]
    assert func.reanalyzed == 2


def test_enabling_pass_stages_live_analysis_limits_before_reanalysis():
    # Given: Binary Ninja has its default per-view analysis budget and a guided trigger.
    events = []
    bv = FakeBv(events)
    func = FakeFunc(events)
    settings = FakeSettings()
    settings.bools[("analysis.guided.triggers.invalidInstruction", func)] = True

    # When: the user enables DispatchThis branch recovery from the UI.
    assert ui.set_function_pass(bv, func, settings_module.BRANCH_TARGETS_SETTING, True, settings)

    # Then: all core limits are staged before the explicit user-requested reanalysis.
    assert events == ["parameters", "reanalyze"]
    parameters = bv.parameters_for_analysis
    assert parameters.maxFunctionSize == 0
    assert parameters.maxFunctionAnalysisTime == 3600000
    assert parameters.maxFunctionUpdateCount == 1024
    assert settings.get_bool("analysis.guided.triggers.invalidInstruction", func) is False


def test_disable_function_settings_clears_all_visible_passes():
    bv = FakeBv()
    func = FakeFunc()
    settings = FakeSettings()

    assert ui.disable_function_settings(bv, func, settings, reanalyze=True)

    assert settings.writes == [
        (key, False, func, ui.SettingsScope.SettingsResourceScope)
        for key in settings_module.PASS_SETTING_IDS
    ]
    assert func.reanalyzed == 1


def test_use_provider_clears_function_evidence_without_storing_provider_identity(monkeypatch):
    bv = FakeBv()
    func = FakeFunc()
    bv.functions = [func]
    func.session_data["dispatchthis_workflow_state"] = {
        "branch": {"receipts": {0x1000: (0x2000,)}},
    }
    calls = []
    monkeypatch.setattr(ui, "active_provider_id", lambda _bv, _settings: None)
    monkeypatch.setattr(
        ui,
        "set_active_provider",
        lambda bv_arg, provider_id, configured: (calls.append((bv_arg, provider_id, configured)), True)[1],
    )
    pending_writes = []
    monkeypatch.setattr(ui, "_pending_reproof_functions", lambda _bv, _settings: frozenset())
    monkeypatch.setattr(
        ui,
        "_set_pending_reproof_functions",
        lambda bv_arg, starts, configured: (pending_writes.append((bv_arg, starts, configured)), True)[1],
    )
    settings = FakeSettings()

    assert ui.use_provider(bv, func, "external", settings)

    assert calls == [(bv, "external", settings)]
    assert pending_writes == [(bv, frozenset({0x1000}), settings)]
    assert func.session_data == {}
    assert func.reanalyzed == 1


def test_invalidating_evidence_removes_condition_failure_tags():
    func = FakeFunc()
    func.session_data["dispatchthis_workflow_state"] = {
        "branch": {
            "conditions": {
                0x1000: {},
                0x2000: {},
            },
        },
    }

    ui._invalidate_function_evidence(func)

    assert func.removed_tags == [
        (0x1000, "DispatchThis Condition Failure"),
        (0x2000, "DispatchThis Condition Failure"),
    ]
    assert func.session_data == {}


def test_register_ui_commands_adds_one_selector_and_seven_pass_commands(monkeypatch):
    FakePluginCommand.registered = []
    monkeypatch.setattr(binaryninja, "PluginCommand", FakePluginCommand, raising=False)
    monkeypatch.setattr(ui, "_schedule_shortcuts", lambda: None)

    ui.register_ui_commands()

    names = [item[0] for item in FakePluginCommand.registered]
    assert names == [
        "DispatchThis\\Select Provider…",
        *[
            f"DispatchThis\\Toggle {settings_module.PASS_LABELS[key]}"
            for key in settings_module.PASS_SETTING_IDS
        ],
        "DispatchThis\\Disable All",
    ]
    assert not any("Profile\\" in name for name in names)


def test_register_shortcuts_sets_key_on_selection_target_action(monkeypatch):
    class FakeKeySequence:
        def __init__(self, text):
            self.text = text

    class FakeUIAction:
        registered = {}

        @classmethod
        def registerAction(cls, name, key_sequence):
            cls.registered[name] = key_sequence.text

    binaryninjaui = types.ModuleType("binaryninjaui")
    binaryninjaui.UIAction = FakeUIAction
    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QKeySequence = FakeKeySequence

    monkeypatch.setitem(sys.modules, "binaryninjaui", binaryninjaui)
    monkeypatch.setitem(sys.modules, "PySide6", types.ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)

    ui._register_shortcuts()

    assert FakeUIAction.registered == {
        "Selection Target\\DispatchThis\\Toggle Indirect Branch Targets": "Alt+Q",
        "Selection Target\\DispatchThis\\Toggle Deflatten": "Alt+W",
        "Selection Target\\DispatchThis\\Toggle String Recovery": "Alt+E",
        "Selection Target\\DispatchThis\\Disable All": "Alt+R",
    }


def test_schedule_shortcuts_runs_registration_on_main_thread_timer(monkeypatch):
    main_thread = []
    scheduled = []
    calls = []

    monkeypatch.setattr(binaryninja, "execute_on_main_thread", lambda callback: main_thread.append(callback), raising=False)

    def register_shortcuts():
        calls.append(True)

    class FakeTimer:
        @staticmethod
        def singleShot(delay, callback):
            scheduled.append((delay, callback))

    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QTimer = FakeTimer

    monkeypatch.setattr(ui, "_register_shortcuts", register_shortcuts)
    monkeypatch.setitem(sys.modules, "PySide6", types.ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)

    ui._schedule_shortcuts()

    assert calls == []
    assert len(main_thread) == 1
    assert scheduled == []
    main_thread[0]()
    assert [delay for delay, _callback in scheduled] == [0, 250, 1000]
    scheduled[0][1]()
    assert len(calls) == 1


def test_register_ui_commands_schedules_shortcuts(monkeypatch):
    FakePluginCommand.registered = []
    scheduled = []

    monkeypatch.setattr(binaryninja, "PluginCommand", FakePluginCommand, raising=False)
    monkeypatch.setattr(ui, "_schedule_shortcuts", lambda: scheduled.append(True))

    ui.register_ui_commands()

    assert scheduled == [True]
