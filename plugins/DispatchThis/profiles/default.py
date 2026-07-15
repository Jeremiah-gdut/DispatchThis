"""Compatibility profile for existing views configured with ``default``."""

from . import dyzznb


PROFILE_ID = "default"
PROFILE_NAME = "Default"
PROFILE_DESCRIPTION = "Compatibility alias for the bundled DYZZNB sample profile."
U48 = dyzznb.U48
CONST_SLOT_TYPE = dyzznb.CONST_SLOT_TYPE

# Keep historical BinaryView settings and profile provenance intact. New sample
# semantics belong in a named profile, not in this compatibility surface.
resolve_branch_gadget = dyzznb.resolve_branch_gadget
resolve_call_gadget = dyzznb.resolve_call_gadget
plan_global_constant_slots = dyzznb.plan_global_constant_slots
plan_deflatten_redirections = dyzznb.plan_deflatten_redirections
