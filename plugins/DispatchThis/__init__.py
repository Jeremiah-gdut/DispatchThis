"""DispatchThis -- IL-level deflattener for an indirect-jump control-flow flattener.

Registers a clone of ``core.function.metaAnalysis`` with plugin activities that
resolve decode-gadget indirect jumps/calls, recover global constants, and optionally deflatten and clean up
obfuscated functions. All passes are opt-in per-function via Function Analysis settings.
"""

import json
from binaryninja import Activity, Workflow, Settings
from .utils.log import log_info, log_warn
from .profiles import register_profile_settings
from .ui import register_ui_commands
from .workflow import (
    resolve_jumps_llil,
    resolve_calls_mlil,
    translate_branches_mlil,
    resolve_globals_mlil,
    string_decrypt_mlil,
    deflatten_mlil,
    cleanup_mlil,
)

# Activity names double as per-function setting identifiers (BN ``eligibility.auto``
# generates a Function Analysis toggle whose ID is the activity name).
RESOLVE_SETTING = "analysis.plugins.dispatchThis.indirectJumpsCalls"
DEFLATTEN_SETTING = "analysis.plugins.dispatchThis.deflatten"
STRING_DECRYPT_SETTING = "analysis.plugins.dispatchThis.stringDecrypt"

# Resolvers run under any feature toggle that needs recovered targets/constants.
_RESOLVER_ELIGIBILITY = {
    "predicates": [
        {"type": "setting", "identifier": RESOLVE_SETTING, "value": True},
        {"type": "setting", "identifier": DEFLATTEN_SETTING, "value": True},
        {"type": "setting", "identifier": STRING_DECRYPT_SETTING, "value": True},
    ],
    "logicalOperator": "or",
}
_RESOLVE_OR_DEFLATTEN = {
    "predicates": [
        {"type": "setting", "identifier": RESOLVE_SETTING, "value": True},
        {"type": "setting", "identifier": DEFLATTEN_SETTING, "value": True},
    ],
    "logicalOperator": "or",
}
_DEFLATTEN_ONLY = {
    "predicates": [
        {"type": "setting", "identifier": DEFLATTEN_SETTING, "value": True},
    ],
}


def register_workflows():
    workflow = Workflow("core.function.metaAnalysis").clone()

    # No-op activity: its auto eligibility surfaces the Indirect Jumps/Calls toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": RESOLVE_SETTING,
        "title": "Indirect Jumps/Calls",
        "description": (
            "Resolve the decode-gadget indirect jumps and calls in this function "
            "(leaves the flattened dispatcher intact)."
        ),
        "eligibility": {"auto": {"default": False}},
    }), action=lambda _ctx: None))

    # Indirect-jump resolver (LLIL), gated on either toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.IndirectPatcher",
        "title": "DispatchThis: Resolve Indirect Jumps",
        "description": "Rewrite decode-gadget jump(reg) into jump(const target).",
        "eligibility": _RESOLVER_ELIGIBILITY,
    }), action=resolve_jumps_llil))
    workflow.insert("core.function.generateMediumLevelIL", [
        RESOLVE_SETTING,
        "extension.DispatchThis.IndirectPatcher",
    ])

    # Indirect-call resolver (MLIL), gated on either toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.IndirectCallPatcher",
        "title": "DispatchThis: Resolve Indirect Calls",
        "description": "Rewrite decode-gadget call(reg) into call(const target).",
        "eligibility": _RESOLVER_ELIGIBILITY,
    }), action=resolve_calls_mlil))

    # Recover if/else shape after indirect branches have been resolved.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.BranchConditionTranslator",
        "title": "DispatchThis: Translate Indirect Branch Conditions",
        "description": "Translate resolved two-target indirect branch switches into if/else branches.",
        "eligibility": _RESOLVE_OR_DEFLATTEN,
    }), action=translate_branches_mlil))

    # Recover read-only semantics for narrow global constant slots before deflattening.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.GlobalConstantResolver",
        "title": "DispatchThis: Resolve Global Constants",
        "description": "Type writable-section global pointer slots as constants when they are used read-only.",
        "eligibility": _RESOLVER_ELIGIBILITY,
    }), action=resolve_globals_mlil))

    # String decrypt (MLIL); auto eligibility surfaces the String Decrypt toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": STRING_DECRYPT_SETTING,
        "title": "String Decrypt",
        "description": "Prepare this function for string decrypt after resolver phases stabilize.",
        "eligibility": {"auto": {"default": False}},
    }), action=string_decrypt_mlil))

    # Deflattener (MLIL); auto eligibility surfaces the Deflatten toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": DEFLATTEN_SETTING,
        "title": "Deflatten",
        "description": (
            "Unflatten this function's control flow by rewriting original basic "
            "block dispatcher jumps into direct gotos. Implies indirect "
            "jump/call resolution."
        ),
        "eligibility": {"auto": {"default": False}},
    }), action=deflatten_mlil))

    # Cleanup (MLIL), gated on the Deflatten toggle only.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.Cleanup",
        "title": "DispatchThis: Cleanup",
        "description": "NOP dispatcher state writes recorded by deflattening.",
        "eligibility": _DEFLATTEN_ONLY,
    }), action=cleanup_mlil))

    workflow.insert("core.function.generateHighLevelIL", [
            "extension.DispatchThis.IndirectCallPatcher",
            "extension.DispatchThis.BranchConditionTranslator",
            "extension.DispatchThis.GlobalConstantResolver",
            STRING_DECRYPT_SETTING,
            DEFLATTEN_SETTING,
            "extension.DispatchThis.Cleanup"
    ])
    workflow.register()
    log_warn("DispatchThis's workflow has been registered!")


# Raise analysis limits for large flattened functions.
Settings().set_integer("analysis.limits.maxFunctionSize", 0)
Settings().set_integer("analysis.limits.expressionValueComputeMaxDepth", 99999)
Settings().set_integer("analysis.limits.maxFunctionAnalysisTime", 600000)
# Keep this finite so non-enrolled complex functions cannot analyze forever.
Settings().set_integer("analysis.limits.maxFunctionUpdateCount", 1024)

# Prevent BN from lowering 32-bit state writes into __builtin_strncpy intrinsics,
# which the MLIL_STORE/SET_VAR matcher won't recognize.
Settings().set_bool("analysis.outlining.builtins", False)

register_profile_settings()
register_workflows()
register_ui_commands(RESOLVE_SETTING, DEFLATTEN_SETTING, STRING_DECRYPT_SETTING)
