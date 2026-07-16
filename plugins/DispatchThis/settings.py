"""Stable Function Analysis setting IDs and their pass dependency closure."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final


BRANCH_TARGETS_SETTING: Final = "analysis.plugins.dispatchThis.branchTargets"
CALL_TARGETS_SETTING: Final = "analysis.plugins.dispatchThis.callTargets"
GLOBAL_DATA_SETTING: Final = "analysis.plugins.dispatchThis.globalData"
BRANCH_CONDITIONS_SETTING: Final = "analysis.plugins.dispatchThis.branchConditions"
CORRELATED_STORES_SETTING: Final = "analysis.plugins.dispatchThis.correlatedStores"
STRING_RECOVERY_SETTING: Final = "analysis.plugins.dispatchThis.stringRecovery"
DEFLATTEN_SETTING: Final = "analysis.plugins.dispatchThis.deflatten"

PASS_SETTING_IDS: Final = (
    BRANCH_TARGETS_SETTING,
    CALL_TARGETS_SETTING,
    GLOBAL_DATA_SETTING,
    BRANCH_CONDITIONS_SETTING,
    CORRELATED_STORES_SETTING,
    STRING_RECOVERY_SETTING,
    DEFLATTEN_SETTING,
)

PASS_DEPENDENCIES: Final = MappingProxyType(
    {
        BRANCH_TARGETS_SETTING: (),
        CALL_TARGETS_SETTING: (BRANCH_TARGETS_SETTING,),
        GLOBAL_DATA_SETTING: (CALL_TARGETS_SETTING,),
        BRANCH_CONDITIONS_SETTING: (GLOBAL_DATA_SETTING,),
        CORRELATED_STORES_SETTING: (GLOBAL_DATA_SETTING,),
        STRING_RECOVERY_SETTING: (),
        DEFLATTEN_SETTING: (BRANCH_CONDITIONS_SETTING,),
    }
)

PASS_LABELS: Final = MappingProxyType(
    {
        BRANCH_TARGETS_SETTING: "Indirect Branch Targets",
        CALL_TARGETS_SETTING: "Indirect Call Targets",
        GLOBAL_DATA_SETTING: "Global Data Semantics",
        BRANCH_CONDITIONS_SETTING: "Branch Condition Translation",
        CORRELATED_STORES_SETTING: "Correlated STORE Recovery",
        STRING_RECOVERY_SETTING: "String Recovery",
        DEFLATTEN_SETTING: "Deflatten",
    }
)


def prerequisites_for(setting: str) -> tuple[str, ...]:
    """Return a pass and all of its prerequisites in enablement order."""

    if setting not in PASS_DEPENDENCIES:
        return ()
    ordered: list[str] = []
    for prerequisite in PASS_DEPENDENCIES[setting]:
        for item in prerequisites_for(prerequisite):
            if item not in ordered:
                ordered.append(item)
    ordered.append(setting)
    return tuple(ordered)


def dependents_for(setting: str) -> tuple[str, ...]:
    """Return a pass and every transitive dependent in stable menu order."""

    if setting not in PASS_DEPENDENCIES:
        return ()
    ordered = [setting]
    for candidate in PASS_SETTING_IDS:
        if setting in prerequisites_for(candidate) and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)
