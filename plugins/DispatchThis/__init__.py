"""DispatchThis -- IL-level deflattener for an indirect-jump control-flow flattener.

Registers a clone of ``core.function.metaAnalysis`` with four plugin activities that
resolve decode-gadget indirect jumps/calls and optionally deflatten and clean up
obfuscated functions. All passes are opt-in per-function via Function Analysis settings.
"""

import json
from binaryninja import Activity, Workflow, Settings
from .utils.log import log_info, log_warn
from .workflow import (
    workflow_resolve_jumps_llil,
    workflow_resolve_calls_mlil,
    workflow_deflatten_mlil,
    workflow_cleanup
)

# Activity names double as per-function setting identifiers (BN ``eligibility.auto``
# generates a Function Analysis toggle whose ID is the activity name).
RESOLVE_SETTING = "analysis.plugins.dispatchThis.indirectJumpsCalls"
DEFLATTEN_SETTING = "analysis.plugins.dispatchThis.deflatten"

# Resolvers run under either toggle: deflattening needs the reconnected CFG.
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
    }), action=lambda analysis_context: None))

    # Indirect-jump resolver (LLIL), gated on either toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.IndirectPatcher",
        "title": "DispatchThis: Resolve Indirect Jumps",
        "description": "Rewrite decode-gadget jump(reg) into jump(const target).",
        "eligibility": _RESOLVE_OR_DEFLATTEN,
    }), action=workflow_resolve_jumps_llil))
    workflow.insert("core.function.generateMediumLevelIL", [
        RESOLVE_SETTING,
        "extension.DispatchThis.IndirectPatcher",
    ])

    # Indirect-call resolver (MLIL), gated on either toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.IndirectCallPatcher",
        "title": "DispatchThis: Resolve Indirect Calls",
        "description": "Rewrite decode-gadget call(reg) into call(const target).",
        "eligibility": _RESOLVE_OR_DEFLATTEN,
    }), action=workflow_resolve_calls_mlil))

    # Deflattener (MLIL); auto eligibility surfaces the Deflatten toggle.
    workflow.register_activity(Activity(json.dumps({
        "name": DEFLATTEN_SETTING,
        "title": "Deflatten",
        "description": (
            "Unflatten this function's control flow by rewriting OBB->dispatcher "
            "jumps into direct gotos. Implies indirect jump/call resolution."
        ),
        "eligibility": {"auto": {"default": False}},
    }), action=workflow_deflatten_mlil))

    # Cleanup (MLIL), gated on the Deflatten toggle only.
    workflow.register_activity(Activity(json.dumps({
        "name": "extension.DispatchThis.Cleanup",
        "title": "DispatchThis: Cleanup",
        "description": "Erase dead decode gadgets and collapse opaque predicates.",
        "eligibility": _DEFLATTEN_ONLY,
    }), action=workflow_cleanup))

    workflow.insert("core.function.generateHighLevelIL", [
            "extension.DispatchThis.IndirectCallPatcher",
            DEFLATTEN_SETTING,
            "extension.DispatchThis.Cleanup"
    ])
    workflow.register()
    log_warn("DispatchThis's workflow has been registered!")


# Raise analysis limits for large flattened functions.
Settings().set_integer("analysis.limits.maxFunctionSize", 0)
Settings().set_integer("analysis.limits.expressionValueComputeMaxDepth", 99999)
Settings().set_integer("analysis.limits.maxFunctionAnalysisTime", 600000)
Settings().set_integer("analysis.limits.maxFunctionUpdateCount", 0)

# Prevent BN from lowering 32-bit state writes into __builtin_strncpy intrinsics,
# which the MLIL_STORE/SET_VAR matcher won't recognize.
Settings().set_bool("analysis.outlining.builtins", False)

register_workflows()
