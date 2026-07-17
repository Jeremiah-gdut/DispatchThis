import runpy
import types

from conftest import ROOT, temporary_modules


SCRIPT_PATH = ROOT / "sample" / "valorant" / "script" / "apply_all_recovery_passes.py"
VALORANT_PROVIDER_ID = "valorant-emdqx-0927cb886ad9a706"
CALL_TARGETS_SETTING = "analysis.plugins.dispatchThis.callTargets"
GLOBAL_DATA_SETTING = "analysis.plugins.dispatchThis.globalData"
STRING_RECOVERY_SETTING = "analysis.plugins.dispatchThis.stringRecovery"


class FakeSettings:
    def __init__(self, enabled=()):
        self.enabled = set(enabled)
        self.writes = []

    def get_bool(self, key, function):
        return (key, function.start) in self.enabled

    def set_bool(self, key, value, function, scope):
        self.writes.append((key, value, function.start, scope))
        self.enabled.add((key, function.start))
        return True


class FakeFunction:
    def __init__(self, start, analysis_skipped=False):
        self.start = start
        self.analysis_skipped = analysis_skipped
        self.reanalyzed = 0

    def reanalyze(self):
        self.reanalyzed += 1


class FakeView:
    def __init__(self, functions):
        self.functions = functions
        self.analysis_updates = 0

    def update_analysis(self):
        self.analysis_updates += 1


def _run_script(view, configured, provider_id):
    binaryninja = types.ModuleType("binaryninja")
    binaryninja.Settings = lambda: configured
    binaryninja.SettingsScope = types.SimpleNamespace(
        SettingsResourceScope="resource"
    )
    package = types.ModuleType("DispatchThis")
    package.__path__ = []
    providers = types.ModuleType("DispatchThis.providers")
    providers.active_provider_id = lambda _view: provider_id
    settings = types.ModuleType("DispatchThis.settings")
    settings.CALL_TARGETS_SETTING = CALL_TARGETS_SETTING
    settings.GLOBAL_DATA_SETTING = GLOBAL_DATA_SETTING
    settings.STRING_RECOVERY_SETTING = STRING_RECOVERY_SETTING
    with temporary_modules(
        {
            "binaryninja": binaryninja,
            "DispatchThis": package,
            "DispatchThis.providers": providers,
            "DispatchThis.settings": settings,
        },
    ):
        return runpy.run_path(SCRIPT_PATH, init_globals={"bv": view})


def test_batch_script_enables_recovery_passes_and_queues_analysis():
    # Given: one active and one analysis-skipped function in the Valorant view.
    active = FakeFunction(0x1000)
    skipped = FakeFunction(0x2000, analysis_skipped=True)
    view = FakeView((active, skipped))
    configured = FakeSettings(((CALL_TARGETS_SETTING, active.start),))

    # When: the batch script runs against the active Valorant provider.
    namespace = _run_script(view, configured, VALORANT_PROVIDER_ID)

    # Then: every requested pass is enabled, only analyzable functions are queued.
    assert configured.writes == [
        (GLOBAL_DATA_SETTING, True, active.start, "resource"),
        (STRING_RECOVERY_SETTING, True, active.start, "resource"),
        (CALL_TARGETS_SETTING, True, skipped.start, "resource"),
        (GLOBAL_DATA_SETTING, True, skipped.start, "resource"),
        (STRING_RECOVERY_SETTING, True, skipped.start, "resource"),
    ]
    assert active.reanalyzed == 1
    assert skipped.reanalyzed == 0
    assert view.analysis_updates == 1
    assert namespace["result"] == {
        "status": "scheduled",
        "provider_id": VALORANT_PROVIDER_ID,
        "functions": 2,
        "reanalyzed": 1,
        "analysis_skipped": 1,
        "enabled": {
            CALL_TARGETS_SETTING: 1,
            GLOBAL_DATA_SETTING: 2,
            STRING_RECOVERY_SETTING: 2,
        },
        "failed": (),
    }


def test_batch_script_refuses_a_non_valorant_provider():
    # Given: a view with a different selected provider.
    function = FakeFunction(0x1000)
    view = FakeView((function,))
    configured = FakeSettings()

    # When: the batch script runs.
    namespace = _run_script(view, configured, "other-provider")

    # Then: it changes neither settings nor analysis scheduling.
    assert configured.writes == []
    assert function.reanalyzed == 0
    assert view.analysis_updates == 0
    assert namespace["result"] == {
        "status": "skipped",
        "provider_id": "other-provider",
        "functions": 0,
        "reanalyzed": 0,
        "analysis_skipped": 0,
        "enabled": {},
        "failed": (),
    }
