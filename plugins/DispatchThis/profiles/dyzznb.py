"""DYZZNB sample profile and its sample-specific recognition rules."""

from binaryninja import TypeClass

from ..helpers import facts, memory, mlil
from ..passes.low.gadget_llil import resolve_llil_jump_plan
from ..passes.medium.deflatten import compute_redirections
from ..passes.medium.indirect_calls import plan_indirect_calls
from ..utils.log import log_info, log_warn


PROFILE_ID = "dyzznb"
PROFILE_NAME = "DYZZNB"
PROFILE_DESCRIPTION = "Rules for the bundled DYZZNB sample profile."
U48 = 0xFFFFFFFFFFFF
CONST_SLOT_TYPE = "uint8_t const* const"

# Supported:
# - branch gadget: custom
# - indirect call gadget: custom
# - global constants: custom
# - correlated stores: omitted
# - deflatten: custom
#
# Validation:
# - raw libdyzznb.so / sub_9641b4: HLIL control predicates are x9 == 1,
#   byte-crypto condition, and i != 0x13; no opaque state-token comparison.


def resolve_branch_gadget(bv, llil, known_targets=None):
    return resolve_llil_jump_plan(bv, llil, known_targets)


def resolve_call_gadget(bv, mlil_func):
    return plan_indirect_calls(bv, mlil_func)


def _plain_ptr_var(bv, addr):
    data_var = bv.get_data_var_at(addr)
    type_ = getattr(data_var, "type", None)
    return (
        getattr(type_, "type_class", None) == TypeClass.PointerTypeClass
        and getattr(type_, "width", None) == 8
        and getattr(getattr(type_, "target", None), "type_class", None)
        == TypeClass.VoidTypeClass
    )


def _plan_global_constant_slot(bv, il, slot_addr, offset):
    if offset == 0 or not _plain_ptr_var(bv, slot_addr):
        return None
    if not memory.in_section(bv, slot_addr, ".data"):
        return None
    value = memory.read_qword_slot(bv, slot_addr)
    if value is None:
        return None
    if not memory.is_mapped_address(bv, (value + offset) & U48):
        return None
    if not mlil.slot_has_no_stores(bv, il, slot_addr, address_mask=U48):
        log_warn(f"[gconst] {hex(slot_addr)}: skipped, slot immutability is unproven")
        return None
    return facts.global_constant_fact(slot_addr, CONST_SLOT_TYPE)


def plan_global_constant_slots(bv, il):
    """Return DYZZNB global-constant facts from its decode shape."""
    if il is None:
        return []

    plans = {}
    for _expr, _use_addr, slot_addr, offset in mlil.iter_load_slot_offsets(
        il,
        address_mask=U48,
    ):
        if slot_addr in plans:
            continue
        plan = _plan_global_constant_slot(bv, il, slot_addr, offset)
        if plan is not None:
            plans[slot_addr] = plan

    out = [plans[addr] for addr in sorted(plans)]
    if out:
        log_info(f"[gconst] planned {len(out)} global constant slot(s)")
    return out


def plan_deflatten_redirections(bv, func, mlil_func):
    return compute_redirections(bv, func, mlil=mlil_func)
