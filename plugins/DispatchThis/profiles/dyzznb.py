"""DYZZNB profile adapter over shared, sample-validated planners."""

from . import default


PROFILE_ID = "dyzznb"
PROFILE_NAME = "DYZZNB"
PROFILE_DESCRIPTION = "Rules for the dyzznb sample profile."
CONST_SLOT_TYPE = "uint8_t const* const"

# Supported:
# - branch gadget: alias default
# - indirect call gadget: alias default
# - global constants: alias default
# - correlated stores: omitted
# - deflatten: alias default
# - string decrypt: alias default
#
# Validation:
# - branch/call/deflatten: validated on sub_924464 and sub_8e09ac in libdyzznb.so
# - global constants: 13 receipts on sub_924464, 5 on sub_8e09ac
# - string decrypt: fixture coverage


resolve_branch_gadget = default.resolve_branch_gadget
resolve_call_gadget = default.resolve_call_gadget
plan_global_constant_slots = default.plan_global_constant_slots
plan_deflatten_redirections = default.plan_deflatten_redirections
plan_string_decrypt_calls = default.plan_string_decrypt_calls
