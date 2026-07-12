"""MLIL-stage global constant slot planner."""

from binaryninja import TypeClass

from ...helpers.facts import global_constant_fact
from ...helpers.memory import in_section, is_mapped_address, read_qword_slot
from ...helpers.mlil import iter_load_slot_offsets, slot_has_no_stores
from ...utils.log import log_info, log_warn


U48 = 0xFFFFFFFFFFFF
CONST_SLOT_TYPE = "uint8_t const* const"


def _plain_ptr_var(bv, addr):
    data_var = bv.get_data_var_at(addr)
    type_ = getattr(data_var, "type", None)
    return (
        getattr(type_, "type_class", None) == TypeClass.PointerTypeClass
        and getattr(type_, "width", None) == 8
        and getattr(getattr(type_, "target", None), "type_class", None)
        == TypeClass.VoidTypeClass
    )


def _plan_for_slot(bv, mlil, slot_addr, offset):
    if offset == 0:
        return None
    if not _plain_ptr_var(bv, slot_addr) or not in_section(bv, slot_addr, ".data"):
        return None
    value = read_qword_slot(bv, slot_addr)
    if value is None:
        return None
    resolved_addr = (value + offset) & U48
    if not is_mapped_address(bv, resolved_addr):
        return None
    if not slot_has_no_stores(bv, mlil, slot_addr, address_mask=U48):
        log_warn(f"[gconst] {hex(slot_addr)}: skipped, slot immutability is unproven")
        return None
    return global_constant_fact(slot_addr, CONST_SLOT_TYPE)


def plan_global_constant_slots(bv, mlil):
    """Find global constant slots whose data-var type should be made const."""
    if mlil is None:
        return []

    plans = {}
    for _expr, _use_addr, slot_addr, offset in iter_load_slot_offsets(mlil, address_mask=U48):
        if slot_addr in plans:
            continue
        plan = _plan_for_slot(bv, mlil, slot_addr, offset)
        if plan is not None:
            plans[slot_addr] = plan

    out = [plans[addr] for addr in sorted(plans)]
    if out:
        log_info(f"[gconst] planned {len(out)} global constant slot(s)")
    return out
