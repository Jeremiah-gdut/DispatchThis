import json

import binaryninja

from conftest import load_plugin_module


class CapturedActivity:
    def __init__(self, config, action=None):
        self.config = config
        self.action = action


class CapturedWorkflow:
    last = None

    def __init__(self, _name):
        self.activities = []
        self.insertions = []
        CapturedWorkflow.last = self

    def clone(self):
        return self

    def register_activity(self, activity):
        self.activities.append(activity)

    def insert(self, *args, **kwargs):
        self.insertions.append((args, kwargs))

    def register(self):
        pass


class CapturedSettings:
    writes = []

    def register_group(self, *_args, **_kwargs):
        return True

    def register_setting(self, *_args, **_kwargs):
        return True

    def set_integer(self, key, value, *_args, **_kwargs):
        self.writes.append(("integer", key, value))
        return True

    def set_bool(self, key, value, *_args, **_kwargs):
        self.writes.append(("bool", key, value))
        return True


class CapturedPluginCommand:
    registered = []

    @classmethod
    def register_for_function(cls, name, description, action, is_valid=None):
        cls.registered.append((name, description, action, is_valid))


def test_plugin_entrypoint_registers_each_pass_with_its_own_setting(monkeypatch):
    monkeypatch.setattr(binaryninja, "Activity", CapturedActivity)
    monkeypatch.setattr(binaryninja, "Workflow", CapturedWorkflow)
    CapturedSettings.writes = []
    monkeypatch.setattr(binaryninja, "Settings", CapturedSettings)
    CapturedPluginCommand.registered = []
    monkeypatch.setattr(binaryninja, "PluginCommand", CapturedPluginCommand, raising=False)

    load_plugin_module("plugins.DispatchThis.__init__")

    configs = {
        json.loads(activity.config)["name"]: json.loads(activity.config)
        for activity in CapturedWorkflow.last.activities
    }
    descriptions = [config["description"] for config in configs.values()]

    assert all("OBB" not in description for description in descriptions)
    assert "extension.DispatchThis.Cleanup" not in configs

    settings = __import__("plugins.DispatchThis.settings", fromlist=["PASS_SETTING_IDS"])
    for setting in settings.PASS_SETTING_IDS:
        assert configs[setting]["eligibility"] == {"auto": {"default": False}}

    activity_settings = {
        "extension.DispatchThis.IndirectPatcher": settings.BRANCH_TARGETS_SETTING,
        "extension.DispatchThis.IndirectCallPatcher": settings.CALL_TARGETS_SETTING,
        "extension.DispatchThis.GlobalConstantResolver": settings.GLOBAL_DATA_SETTING,
        "extension.DispatchThis.BranchConditionTranslator": settings.BRANCH_CONDITIONS_SETTING,
        "extension.DispatchThis.CorrelatedStoreRecovery": settings.CORRELATED_STORES_SETTING,
        "extension.DispatchThis.StringRecovery": settings.STRING_RECOVERY_SETTING,
        "extension.DispatchThis.Deflatten": settings.DEFLATTEN_SETTING,
    }
    for activity_id, setting in activity_settings.items():
        assert configs[activity_id]["eligibility"] == {
            "predicates": [{"type": "setting", "identifier": setting, "value": True}],
            "logicalOperator": "and",
        }

    high_level = next(
        args[1]
        for args, _kwargs in CapturedWorkflow.last.insertions
        if args[0] == "core.function.generateHighLevelIL"
    )
    assert high_level == [
        settings.CALL_TARGETS_SETTING,
        "extension.DispatchThis.IndirectCallPatcher",
        settings.GLOBAL_DATA_SETTING,
        "extension.DispatchThis.GlobalConstantResolver",
        settings.BRANCH_CONDITIONS_SETTING,
        "extension.DispatchThis.BranchConditionTranslator",
        settings.CORRELATED_STORES_SETTING,
        "extension.DispatchThis.CorrelatedStoreRecovery",
        settings.STRING_RECOVERY_SETTING,
        "extension.DispatchThis.StringRecovery",
        settings.DEFLATTEN_SETTING,
        "extension.DispatchThis.Deflatten",
    ]
    assert "extension.DispatchThis.Cleanup" not in high_level
    assert CapturedSettings.writes == []
    names = [item[0] for item in CapturedPluginCommand.registered]
    assert names[0] == "DispatchThis\\Select Provider…"
    assert len([name for name in names if name.startswith("DispatchThis\\Toggle ")]) == 7
    assert "DispatchThis\\Disable All" in names
