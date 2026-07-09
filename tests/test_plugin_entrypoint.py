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
    integer_values = {}

    def register_group(self, *_args, **_kwargs):
        return True

    def register_setting(self, *_args, **_kwargs):
        return True

    def set_integer(self, key, value, *_args, **_kwargs):
        self.integer_values[key] = value

    def set_bool(self, *_args, **_kwargs):
        pass


class CapturedPluginCommand:
    registered = []

    @classmethod
    def register_for_function(cls, name, description, action, is_valid=None):
        cls.registered.append((name, description, action, is_valid))


def test_plugin_entrypoint_uses_glossary_terms_in_user_facing_activity_text(monkeypatch):
    monkeypatch.setattr(binaryninja, "Activity", CapturedActivity)
    monkeypatch.setattr(binaryninja, "Workflow", CapturedWorkflow)
    CapturedSettings.integer_values = {}
    monkeypatch.setattr(binaryninja, "Settings", CapturedSettings)
    CapturedPluginCommand.registered = []
    monkeypatch.setattr(binaryninja, "PluginCommand", CapturedPluginCommand, raising=False)

    plugin = load_plugin_module("plugins.DispatchThis.__init__")

    configs = {
        json.loads(activity.config)["name"]: json.loads(activity.config)
        for activity in CapturedWorkflow.last.activities
    }
    descriptions = [config["description"] for config in configs.values()]

    assert all("OBB" not in description for description in descriptions)
    assert configs[plugin.STRING_DECRYPT_SETTING]["title"] == "String Decrypt"
    assert configs[plugin.STRING_DECRYPT_SETTING]["eligibility"] == {"auto": {"default": False}}

    resolver_ids = [
        "extension.DispatchThis.IndirectPatcher",
        "extension.DispatchThis.IndirectCallPatcher",
        "extension.DispatchThis.GlobalConstantResolver",
    ]
    for activity_id in resolver_ids:
        identifiers = {
            predicate["identifier"]
            for predicate in configs[activity_id]["eligibility"]["predicates"]
        }
        assert plugin.STRING_DECRYPT_SETTING in identifiers
    branch_translation_identifiers = {
        predicate["identifier"]
        for predicate in configs["extension.DispatchThis.BranchConditionTranslator"]["eligibility"]["predicates"]
    }
    assert plugin.STRING_DECRYPT_SETTING not in branch_translation_identifiers

    high_level = next(
        args[1]
        for args, _kwargs in CapturedWorkflow.last.insertions
        if args[0] == "core.function.generateHighLevelIL"
    )
    assert high_level.index("extension.DispatchThis.GlobalConstantResolver") < high_level.index(
        plugin.STRING_DECRYPT_SETTING
    )
    assert high_level.index(plugin.STRING_DECRYPT_SETTING) < high_level.index(
        plugin.DEFLATTEN_SETTING
    )
    assert CapturedSettings.integer_values["analysis.limits.maxFunctionUpdateCount"] == 1024
    names = [item[0] for item in CapturedPluginCommand.registered]
    assert "DispatchThis\\Toggle Resolver" in names
    assert "DispatchThis\\Disable All" in names
