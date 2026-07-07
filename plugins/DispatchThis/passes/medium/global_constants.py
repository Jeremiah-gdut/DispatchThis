"""MLIL-stage global constant slot planner."""

from ...helpers.facts import global_constant_fact
from ...helpers.memory import in_section, is_valid_target, read_qword_slot
from ...helpers.mlil import iter_load_slot_offsets, mlil_stores_to_address
from ...utils.log import log_info, log_warn


U48 = 0xFFFFFFFFFFFF
CONST_SLOT_TYPE = "uint8_t const* const"


def _plain_ptr_var(bv, addr):
    data_var = bv.get_data_var_at(addr)
    return data_var is not None and str(data_var.type).replace(" ", "") == "void*"


def _ref_functions(bv, data_var):
    seen = set()
    for ref in list(getattr(data_var, "code_refs", ()) or ()):
        funcs = []
        func = getattr(ref, "function", None)
        if func is not None:
            funcs = [func]
        else:
            try:
                funcs = list(bv.get_functions_containing(ref.address))
            except Exception:  # noqa: BLE001
                funcs = []
        for func in funcs:
            key = getattr(func, "start", id(func))
            if key in seen:
                continue
            seen.add(key)
            yield func


def _refs_store_slot(bv, current_mlil, slot_addr):
    data_var = bv.get_data_var_at(slot_addr)
    if mlil_stores_to_address(current_mlil, slot_addr, address_mask=U48):
        return True
    for func in _ref_functions(bv, data_var):
        mlil = getattr(func, "mlil", None)
        if (
            mlil is not None
            and mlil is not current_mlil
            and mlil_stores_to_address(mlil, slot_addr, address_mask=U48)
        ):
            return True
    return False


def _plan_for_slot(bv, mlil, slot_addr, offset, use_addr):
    if offset == 0:
        return None
    if not _plain_ptr_var(bv, slot_addr) or not in_section(bv, slot_addr, ".data"):
        return None
    value = read_qword_slot(bv, slot_addr)
    if value is None:
        return None
    resolved_addr = (value + offset) & U48
    if not is_valid_target(bv, resolved_addr):
        return None
    if _refs_store_slot(bv, mlil, slot_addr):
        log_warn(f"[gconst] {hex(slot_addr)}: skipped, known reference writes to slot")
        return None
    return global_constant_fact(slot_addr, CONST_SLOT_TYPE, value, resolved_addr, use_addr)


def plan_global_constant_slots(bv, mlil):
    """Find global constant slots whose data-var type should be made const."""
    if mlil is None:
        return []

    plans = {}
    for _expr, use_addr, slot_addr, offset in iter_load_slot_offsets(mlil, address_mask=U48):
        if slot_addr in plans:
            continue
        plan = _plan_for_slot(bv, mlil, slot_addr, offset, use_addr)
        if plan is not None:
            plans[slot_addr] = plan

    out = [plans[addr] for addr in sorted(plans)]
    if out:
        log_info(f"[gconst] planned {len(out)} global constant slot(s)")
    return out
