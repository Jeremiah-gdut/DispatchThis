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
        CapturedWorkflow.last = self

    def clone(self):
        return self

    def register_activity(self, activity):
        self.activities.append(activity)

    def insert(self, *_args, **_kwargs):
        pass

    def register(self):
        pass


class CapturedSettings:
    def register_group(self, *_args, **_kwargs):
        return True

    def register_setting(self, *_args, **_kwargs):
        return True

    def set_integer(self, *_args, **_kwargs):
        pass

    def set_bool(self, *_args, **_kwargs):
        pass


def test_plugin_entrypoint_uses_glossary_terms_in_user_facing_activity_text(monkeypatch):
    monkeypatch.setattr(binaryninja, "Activity", CapturedActivity)
    monkeypatch.setattr(binaryninja, "Workflow", CapturedWorkflow)
    monkeypatch.setattr(binaryninja, "Settings", CapturedSettings)

    load_plugin_module("plugins.DispatchThis.__init__")

    descriptions = [
        json.loads(activity.config)["description"]
        for activity in CapturedWorkflow.last.activities
    ]

    assert all("OBB" not in description for description in descriptions)
