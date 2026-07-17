import sys
import types

from conftest import load_plugin_module


class FakeSettings:
    def __init__(self):
        self.bools = {}
        self.strings = {}
        self.writes = []

    def get_bool(self, key, resource=None):
        return self.bools.get((key, resource), False)

    def set_bool(self, key, value, resource=None, scope=None):
        self.bools[(key, resource)] = value
        self.writes.append(("bool", key, value, resource, scope))
        return True

    def get_string(self, key, resource=None):
        default = "[]" if key == "analysis.plugins.dispatchThis.providerReproofPendingFunctions" else ""
        return self.strings.get((key, resource), default)

    def set_string(self, key, value, resource=None, scope=None):
        self.strings[(key, resource)] = value
        self.writes.append(("string", key, value, resource, scope))
        return True


class FakeFunction:
    def __init__(self, start):
        self.name = f"sub_{start:x}"
        self.start = start
        self.reanalyzed = 0
        self.session_data = {}

    def reanalyze(self):
        self.reanalyzed += 1


class FakeView:
    def __init__(self, functions):
        self.functions = functions
        self.session_data = {"dispatchthis_mlil_stable": {func.start: True for func in functions}}


def test_enabling_deflatten_enables_only_its_transitive_prerequisites():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()

    assert ui.set_function_pass(bv, func, settings.DEFLATTEN_SETTING, True, configured, reanalyze=False)

    enabled_keys = [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and value and resource is func
    ]
    assert enabled_keys == [
        settings.BRANCH_TARGETS_SETTING,
        settings.CALL_TARGETS_SETTING,
        settings.GLOBAL_DATA_SETTING,
        settings.BRANCH_CONDITIONS_SETTING,
        settings.DEFLATTEN_SETTING,
    ]
    assert settings.CORRELATED_STORES_SETTING not in enabled_keys
    assert settings.STRING_RECOVERY_SETTING not in enabled_keys


def test_enabling_call_targets_has_no_prerequisites():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()

    assert ui.set_function_pass(
        bv,
        func,
        settings.CALL_TARGETS_SETTING,
        True,
        configured,
        reanalyze=False,
    )

    assert [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and value and resource is func
    ] == [settings.CALL_TARGETS_SETTING]


def test_enabling_global_data_has_no_prerequisites():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()

    assert ui.set_function_pass(
        bv,
        func,
        settings.GLOBAL_DATA_SETTING,
        True,
        configured,
        reanalyze=False,
    )

    assert [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and value and resource is func
    ] == [settings.GLOBAL_DATA_SETTING]


def test_enabling_correlated_stores_preserves_its_required_recovery_passes():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()

    assert ui.set_function_pass(
        bv,
        func,
        settings.CORRELATED_STORES_SETTING,
        True,
        configured,
        reanalyze=False,
    )

    assert [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and value and resource is func
    ] == [
        settings.BRANCH_TARGETS_SETTING,
        settings.CALL_TARGETS_SETTING,
        settings.GLOBAL_DATA_SETTING,
        settings.CORRELATED_STORES_SETTING,
    ]


def test_disabling_global_data_leaves_string_recovery_enabled():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()
    for key in settings.PASS_SETTING_IDS:
        configured.bools[(key, func)] = True

    assert ui.set_function_pass(bv, func, settings.GLOBAL_DATA_SETTING, False, configured, reanalyze=False)

    disabled_keys = [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and not value and resource is func
    ]
    assert disabled_keys == [
        settings.GLOBAL_DATA_SETTING,
        settings.CORRELATED_STORES_SETTING,
        settings.DEFLATTEN_SETTING,
    ]
    assert configured.get_bool(settings.BRANCH_TARGETS_SETTING, func)
    assert configured.get_bool(settings.CALL_TARGETS_SETTING, func)
    assert configured.get_bool(settings.STRING_RECOVERY_SETTING, func)


def test_disabling_branch_leaves_independent_call_and_global_passes_enabled():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()
    for key in settings.PASS_SETTING_IDS:
        configured.bools[(key, func)] = True

    assert ui.set_function_pass(
        bv,
        func,
        settings.BRANCH_TARGETS_SETTING,
        False,
        configured,
        reanalyze=False,
    )

    disabled_keys = [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and not value and resource is func
    ]
    assert disabled_keys == [
        settings.BRANCH_TARGETS_SETTING,
        settings.BRANCH_CONDITIONS_SETTING,
        settings.CORRELATED_STORES_SETTING,
        settings.DEFLATTEN_SETTING,
    ]
    assert configured.get_bool(settings.CALL_TARGETS_SETTING, func)
    assert configured.get_bool(settings.GLOBAL_DATA_SETTING, func)


def test_enabling_string_recovery_has_no_prerequisites():
    ui = load_plugin_module("plugins.DispatchThis.ui")
    settings = load_plugin_module("plugins.DispatchThis.settings")
    func = FakeFunction(0x1000)
    bv = FakeView([func])
    configured = FakeSettings()

    assert ui.set_function_pass(bv, func, settings.STRING_RECOVERY_SETTING, True, configured, reanalyze=False)

    assert [
        key
        for kind, key, value, resource, _scope in configured.writes
        if kind == "bool" and value and resource is func
    ] == [settings.STRING_RECOVERY_SETTING]


def test_provider_change_is_view_scoped_and_clears_all_function_evidence():
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    providers = load_plugin_module("plugins.DispatchThis.providers")
    ui = load_plugin_module("plugins.DispatchThis.ui")
    first = FakeFunction(0x1000)
    second = FakeFunction(0x2000)
    bv = FakeView([first, second])
    configured = FakeSettings()
    first.session_data["dispatchthis_workflow_state"] = {"branch": {"receipts": {0x1000: (0x2000,)}}}
    second.session_data["dispatchthis_workflow_state"] = {"call": {"receipts": {0x2000: 0x3000}}}
    provider = semantics.SampleSemantics(
        provider_id="ui-provider",
        name="UI provider",
        api_version=semantics.CORE_API_VERSION,
    )
    assert providers.register_provider(provider)

    assert ui.use_provider(bv, first, provider.provider_id, configured)

    assert configured.writes[:2] == [
        (
            "string",
            providers._PROVIDER_REPROOF_PENDING_FUNCTIONS_SETTING,
            "[4096,8192]",
            bv,
            providers.SettingsScope.SettingsResourceScope,
        ),
        (
            "string",
            providers.ACTIVE_PROVIDER_SETTING,
            provider.provider_id,
            bv,
            providers.SettingsScope.SettingsResourceScope,
        ),
    ]
    assert "dispatchthis_workflow_state" not in first.session_data
    assert "dispatchthis_workflow_state" not in second.session_data
    assert bv.session_data["dispatchthis_mlil_stable"] == {}
    assert configured.get_string(providers._PROVIDER_REPROOF_PENDING_FUNCTIONS_SETTING, bv) == "[4096,8192]"
    assert first.reanalyzed == 1


def test_provider_picker_displays_names_but_persists_the_stable_id(monkeypatch):
    semantics = load_plugin_module("plugins.DispatchThis.semantics")
    providers = load_plugin_module("plugins.DispatchThis.providers")
    ui = load_plugin_module("plugins.DispatchThis.ui")
    first = semantics.SampleSemantics(
        provider_id="picker-first",
        name="First Provider",
        api_version=semantics.CORE_API_VERSION,
    )
    second = semantics.SampleSemantics(
        provider_id="picker-second",
        name="Second Provider",
        api_version=semantics.CORE_API_VERSION,
    )
    assert providers.register_provider(first)
    assert providers.register_provider(second)
    choices = []
    selected = []
    monkeypatch.setitem(
        sys.modules,
        "binaryninja.interaction",
        types.SimpleNamespace(
            get_choice_input=lambda _title, _prompt, values: choices.append(values) or 1,
        ),
    )
    monkeypatch.setattr(ui, "use_provider", lambda _bv, _func, provider_id: selected.append(provider_id) or True)

    assert ui.select_provider(FakeView([]), FakeFunction(0x1000))

    assert choices == [["First Provider (picker-first)", "Second Provider (picker-second)"]]
    assert selected == ["picker-second"]
