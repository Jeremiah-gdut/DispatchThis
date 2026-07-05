from ..passes.low.gadget_llil import resolve_llil_jump_plan
from ..passes.medium.global_constants import plan_global_constant_slots as _plan_global_constant_slots
from ..passes.medium.indirect_calls import plan_indirect_calls
from ..passes.medium.string_decrypt import plan_string_decrypt_calls as _plan_string_decrypt_calls


PROFILE_ID = "default"
PROFILE_NAME = "Default"
PROFILE_DESCRIPTION = "Built-in rules for the current DispatchThis sample family."


def resolve_branch_gadget(bv, llil, known_targets=None):
    return resolve_llil_jump_plan(bv, llil, known_targets)


def resolve_call_gadget(bv, mlil):
    return plan_indirect_calls(bv, mlil)


def plan_global_constant_slots(bv, mlil):
    return _plan_global_constant_slots(bv, mlil)


def plan_string_decrypt_calls(bv, func, mlil, mlil_stable):
    return _plan_string_decrypt_calls(bv, func, mlil, mlil_stable)
