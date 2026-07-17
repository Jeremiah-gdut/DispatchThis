"""Enable the independent Valorant recovery passes for every function.

Run from an open Valorant BinaryView:

    bn py exec --target active --script sample/valorant/script/apply_all_recovery_passes.py
"""

from typing import Final, TypedDict

from binaryninja import Settings, SettingsScope

from DispatchThis.providers import active_provider_id
from DispatchThis.settings import (
    CALL_TARGETS_SETTING,
    GLOBAL_DATA_SETTING,
    STRING_RECOVERY_SETTING,
)


VALORANT_PROVIDER_ID: Final = "valorant-emdqx-0927cb886ad9a706"
RECOVERY_SETTINGS: Final = (
    CALL_TARGETS_SETTING,
    GLOBAL_DATA_SETTING,
    STRING_RECOVERY_SETTING,
)


class BatchApplyResult(TypedDict):
    status: str
    provider_id: str
    functions: int
    reanalyzed: int
    analysis_skipped: int
    enabled: dict[str, int]
    failed: tuple[tuple[int, str], ...]


def apply_to_all_functions(view) -> BatchApplyResult:
    """Enable the three independent passes, then enqueue one analysis batch."""

    provider_id = active_provider_id(view)
    if provider_id != VALORANT_PROVIDER_ID:
        return {
            "status": "skipped",
            "provider_id": provider_id,
            "functions": 0,
            "reanalyzed": 0,
            "analysis_skipped": 0,
            "enabled": {},
            "failed": (),
        }

    configured = Settings()
    functions = tuple(view.functions)
    enabled = {setting: 0 for setting in RECOVERY_SETTINGS}
    failed = []
    ready = []

    for function in functions:
        function_ready = True
        for setting in RECOVERY_SETTINGS:
            if configured.get_bool(setting, function):
                continue
            if configured.set_bool(
                setting,
                True,
                function,
                SettingsScope.SettingsResourceScope,
            ):
                enabled[setting] += 1
                continue
            function_ready = False
            failed.append((function.start, setting))
        if function_ready:
            ready.append(function)

    reanalyzed = 0
    analysis_skipped = 0
    for function in ready:
        if function.analysis_skipped:
            analysis_skipped += 1
            continue
        function.reanalyze()
        reanalyzed += 1
    if reanalyzed:
        view.update_analysis()

    return {
        "status": "scheduled",
        "provider_id": provider_id,
        "functions": len(functions),
        "reanalyzed": reanalyzed,
        "analysis_skipped": analysis_skipped,
        "enabled": enabled,
        "failed": tuple(failed),
    }


# ``bn py exec`` injects the selected BinaryView into the script globals.
result = apply_to_all_functions(globals()["bv"])
print(
    "[valorant:batch] "
    f"{result['status']}: {result['reanalyzed']}/{result['functions']} functions queued"
)
