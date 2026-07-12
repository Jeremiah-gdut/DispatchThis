"""DYZZNB profile adapter over shared, sample-validated planners."""

from . import default


PROFILE_ID = "dyzznb"
PROFILE_NAME = "DYZZNB"
PROFILE_DESCRIPTION = "Rules for the dyzznb sample profile."
CONST_SLOT_TYPE = "uint8_t const* const"

# Supported:
# - branch gadget: shared default planner
# - indirect call gadget: shared default planner
# - global constants: shared default planner
# - correlated stores: intentional no-op
# - deflatten: shared default planner
# - string decrypt: shared default planner
#
# Validation:
# - branch/call/deflatten: validated on sub_924464 and sub_8e09ac in libdyzznb.so
# - global constants: 13 receipts on sub_924464, 5 on sub_8e09ac
# - string decrypt: fixture coverage


def resolve_branch_gadget(bv, llil, known_targets=None):
    return default.resolve_branch_gadget(bv, llil, known_targets)


def resolve_call_gadget(bv, mlil):
    return default.resolve_call_gadget(bv, mlil)


def plan_global_constant_slots(bv, mlil):
    return default.plan_global_constant_slots(bv, mlil)


def plan_correlated_store_rewrites(_bv, _func, _mlil):
    return []


def plan_deflatten_redirections(bv, func, mlil):
    return default.plan_deflatten_redirections(bv, func, mlil)


def plan_string_decrypt_calls(bv, func, mlil, mlil_stable):
    return default.plan_string_decrypt_calls(bv, func, mlil, mlil_stable)
