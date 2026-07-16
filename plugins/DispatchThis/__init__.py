"""DispatchThis core workflow and external SampleSemantics registration API."""

from __future__ import annotations

import json

from binaryninja import Activity, Workflow

from .profiles import get_profile, profile_ids
from .providers import _register_legacy_profile, register_provider, register_provider_settings
from .semantics import (
    CORE_API_VERSION,
    BranchTargetFact,
    BranchTargetQuery,
    CallTargetFact,
    CallTargetQuery,
    CompleteBatch,
    CorrelatedStoreArm,
    CorrelatedStorePlan,
    CorrelatedStoreQuery,
    DeflattenConditionWitness,
    DeflattenPlan,
    DeflattenPlanKind,
    DeflattenRedirection,
    DeflattenQuery,
    DeflattenStateToken,
    DeflattenStateWriteWitness,
    GlobalDataFact,
    GlobalDataQuery,
    Inconclusive,
    ProviderContractError,
    SampleSemantics,
    StringRecoveryFact,
    StringRecoveryQuery,
)
from .helpers.values import (
    AnalysisBudget,
    CompleteValues,
    DefinitionGraph,
    Handled,
    NotHandled,
    PathSource,
    ValueCase,
    ValuePolicy,
    evaluate_values,
)
from .settings import PASS_LABELS, PASS_SETTING_IDS
from .ui import register_ui_commands
from .utils.log import log_warn
from .workflow import (
    deflatten_mlil,
    recover_phi_stores_mlil,
    resolve_calls_mlil,
    resolve_globals_mlil,
    resolve_jumps_llil,
    string_decrypt_mlil,
    translate_branches_mlil,
)


def _eligible_when_enabled(setting: str) -> dict[str, list[dict[str, str | bool]] | str]:
    return {
        "predicates": [{"type": "setting", "identifier": setting, "value": True}],
        "logicalOperator": "and",
    }


def _register_pass_toggle(workflow: Workflow, setting: str) -> None:
    workflow.register_activity(
        Activity(
            json.dumps(
                {
                    "name": setting,
                    "title": PASS_LABELS[setting],
                    "description": f"Enable DispatchThis {PASS_LABELS[setting]} for this function.",
                    "eligibility": {"auto": {"default": False}},
                }
            ),
            action=lambda _ctx: None,
        )
    )


def _register_activity(
    workflow: Workflow,
    name: str,
    title: str,
    description: str,
    setting: str,
    action,
) -> None:
    workflow.register_activity(
        Activity(
            json.dumps(
                {
                    "name": name,
                    "title": title,
                    "description": description,
                    "eligibility": _eligible_when_enabled(setting),
                }
            ),
            action=action,
        )
    )


def _register_legacy_providers() -> None:
    """Keep bundled profiles working only through the private migration path."""

    for profile_id in profile_ids():
        _register_legacy_profile(get_profile(profile_id))


def register_workflows() -> None:
    """Clone core analysis and register seven independently gated pass slots."""

    workflow = Workflow("core.function.metaAnalysis").clone()
    for setting in PASS_SETTING_IDS:
        _register_pass_toggle(workflow, setting)

    branch, call, global_data, condition, stores, strings, deflatten = PASS_SETTING_IDS
    _register_activity(
        workflow,
        "extension.DispatchThis.IndirectPatcher",
        "DispatchThis: Resolve Indirect Branch Targets",
        "Validate provider branch facts and submit exact indirect branch targets.",
        branch,
        resolve_jumps_llil,
    )
    _register_activity(
        workflow,
        "extension.DispatchThis.IndirectCallPatcher",
        "DispatchThis: Resolve Indirect Call Targets",
        "Recover provider-proven indirect call targets.",
        call,
        resolve_calls_mlil,
    )
    _register_activity(
        workflow,
        "extension.DispatchThis.GlobalConstantResolver",
        "DispatchThis: Resolve Global Data Semantics",
        "Apply provider-proven global-data semantics.",
        global_data,
        resolve_globals_mlil,
    )
    _register_activity(
        workflow,
        "extension.DispatchThis.BranchConditionTranslator",
        "DispatchThis: Translate Branch Conditions",
        "Translate proven indirect branch conditions after upstream recovery settles.",
        condition,
        translate_branches_mlil,
    )
    _register_activity(
        workflow,
        "extension.DispatchThis.CorrelatedStoreRecovery",
        "DispatchThis: Recover Correlated STOREs",
        "Apply provider-proven path-correlated STORE recovery plans.",
        stores,
        recover_phi_stores_mlil,
    )
    _register_activity(
        workflow,
        "extension.DispatchThis.StringRecovery",
        "DispatchThis: Recover Strings",
        "Apply provider-proven string recovery facts.",
        strings,
        string_decrypt_mlil,
    )
    _register_activity(
        workflow,
        "extension.DispatchThis.Deflatten",
        "DispatchThis: Deflatten",
        "Apply provider-proven dispatcher redirection plans.",
        deflatten,
        deflatten_mlil,
    )

    workflow.insert(
        "core.function.generateMediumLevelIL",
        [branch, "extension.DispatchThis.IndirectPatcher"],
    )
    workflow.insert_after(
        "core.function.findStringReferences",
        [
            strings,
            "extension.DispatchThis.StringRecovery",
        ],
    )
    workflow.insert(
        "core.function.generateHighLevelIL",
        [
            call,
            "extension.DispatchThis.IndirectCallPatcher",
            global_data,
            "extension.DispatchThis.GlobalConstantResolver",
            condition,
            "extension.DispatchThis.BranchConditionTranslator",
            stores,
            "extension.DispatchThis.CorrelatedStoreRecovery",
            deflatten,
            "extension.DispatchThis.Deflatten",
        ],
    )
    workflow.register()
    log_warn("DispatchThis's workflow has been registered!")


register_provider_settings()
_register_legacy_providers()
register_workflows()
register_ui_commands()


__all__ = (
    "CORE_API_VERSION",
    "BranchTargetFact",
    "BranchTargetQuery",
    "CallTargetFact",
    "CallTargetQuery",
    "CompleteBatch",
    "CorrelatedStoreArm",
    "CorrelatedStorePlan",
    "CorrelatedStoreQuery",
    "DeflattenConditionWitness",
    "DeflattenPlan",
    "DeflattenPlanKind",
    "DeflattenRedirection",
    "DeflattenQuery",
    "DeflattenStateToken",
    "DeflattenStateWriteWitness",
    "GlobalDataFact",
    "GlobalDataQuery",
    "Inconclusive",
    "ProviderContractError",
    "SampleSemantics",
    "StringRecoveryFact",
    "StringRecoveryQuery",
    "AnalysisBudget",
    "CompleteValues",
    "DefinitionGraph",
    "Handled",
    "NotHandled",
    "PathSource",
    "ValueCase",
    "ValuePolicy",
    "evaluate_values",
    "register_provider",
)
