"""
LLIL-stage jump gadget resolver that replaces indirect jump expressions
with replace_expr and an associated jump to the original address 
"""

from ...helpers.facts import branch_fact
from ...helpers.llil import (
    LOAD_OPS,
    U48,
    const_values,
    iter_indirect_jumps as iter_llil_indirect_jumps,
    peel_reg_definition,
)
from ...helpers.memory import read_u64le
from ...utils.log import log_debug, log_warn, log_error


UNRESOLVED_INDIRECT_TAG = "Unresolved Indirect Control Flow"


def clear_resolved_indirect_branch_tags(func):
    seen = set()
    for branch in func.indirect_branches:
        if branch.auto_defined:
            continue
        if branch.source_addr in seen:
            continue
        seen.add(branch.source_addr)
        func.remove_auto_address_tags_of_type(
            branch.source_addr,
            UNRESOLVED_INDIRECT_TAG,
        )

# --------------------------------------------------------------------------- #
# table decode (closed form)
# --------------------------------------------------------------------------- #

def get_qword_at(bv, addr):
    """Read a little-endian qword from image memory. ``None`` if unmapped."""
    return read_u64le(bv, addr & U48)


def resolve_indirect_jump_addr(bv, slot, entry_offset, table_base_key, key):
    """Decode one target from the relocated jump table.

        table_base = (*slot + table_base_key) mod 2^48
        entry      = *(table_base + entry_offset)
        target     = (entry + key) mod 2^48
    """
    encoded_table_base = get_qword_at(bv, slot)
    if encoded_table_base is None:
        return None
    table_base = (encoded_table_base + table_base_key) & U48
    entry_addr = (table_base + entry_offset) & U48
    encoded_target = get_qword_at(bv, entry_addr)
    if encoded_target is None:
        return None
    return (encoded_target + key) & U48


# --------------------------------------------------------------------------- #
# gadget parse + drive
# --------------------------------------------------------------------------- #

def _single_const(bv, ssa, expr):
    values = const_values(bv, ssa, expr)
    return next(iter(values)) if len(values) == 1 else None


def _slot_from_load(bv, ssa, sload):
    if sload is None or sload.operation.name not in LOAD_OPS:
        return None
    sp = sload.src
    if sp.operation.name in ("LLIL_CONST_PTR", "LLIL_CONST"):
        return sp.constant & U48
    return _single_const(bv, ssa, sp)


def _slot_add_const_candidates(bv, ssa, expr):
    expr = peel_reg_definition(ssa, expr)
    if expr is None or expr.operation.name != "LLIL_ADD":
        return []
    candidates = []
    for sload_expr, const_expr in ((expr.left, expr.right), (expr.right, expr.left)):
        slot = _slot_from_load(bv, ssa, peel_reg_definition(ssa, sload_expr))
        if slot is None:
            continue
        value = _single_const(bv, ssa, const_expr)
        if value is not None:
            candidates.append((slot, value & U48))
    return candidates


def _valid_offsets(bv, slot, table_base_key, key, offsets):
    valid = set()
    for offset in offsets:
        target = resolve_indirect_jump_addr(bv, slot, offset, table_base_key, key)
        if target is not None and bv.is_valid_offset(target):
            valid.add(offset & U48)
    return valid


def _is_index_offset(ssa, expr):
    expr = peel_reg_definition(ssa, expr)
    return expr is not None and expr.operation.name in ("LLIL_LSL", "LLIL_LSR")


def _jump_parts(bv, ssa, jump_il):
    """
    
    Walk the decode gadget feeding ``jump_il`` backwards through LLIL SSA.

    Returns ``(slot, table_base_key, key, offsets)`` as ints, or ``None`` if the
    gadget does not match the expected shape."""

    jdest = jump_il.ssa_form.dest
    if jdest.operation.name != "LLIL_REG_SSA":
        return None

    # rax = rax (+/-) KEY -- the final decode step is `add` or `sub`; for `sub`
    # the effective key is negated (the U48 decode math treats it as addition).
    fin = peel_reg_definition(ssa, jdest)
    if fin is None or fin.operation.name not in ("LLIL_ADD", "LLIL_SUB"):
        return None
    if fin.operation.name == "LLIL_SUB":
        chain_expr, key_expr, neg = fin.left, fin.right, True
    else:
        # commutative: the chain operand leads to the entry LOAD; other is key.
        if peel_reg_definition(ssa, fin.left).operation.name in LOAD_OPS:
            chain_expr, key_expr, neg = fin.left, fin.right, False
        else:
            chain_expr, key_expr, neg = fin.right, fin.left, False
    key = _single_const(bv, ssa, key_expr)
    if key is None:
        return None
    if neg:
        key = -key

    # rax = [table_base_key + rax] -- entry load. The table_base_key (a register in
    # LLIL) and the table-base chain can sit on either side of the address add;
    # the chain is the operand whose definition is the OFFSET + [&SLOT] add.
    load = peel_reg_definition(ssa, chain_expr)
    if load is None or load.operation.name not in LOAD_OPS:
        return None
    addr = load.src
    if addr.operation.name != "LLIL_ADD":
        return None

    # Some ARM64 samples pre-add the table-base key before indexing:
    #     target = *(*slot + table_base_key + offset) + key
    for tb, disp_expr in (
        (peel_reg_definition(ssa, addr.left), addr.right),
        (peel_reg_definition(ssa, addr.right), addr.left),
    ):
        if not _is_index_offset(ssa, disp_expr):
            continue
        for slot, table_base_key in _slot_add_const_candidates(bv, ssa, tb):
            offsets = {o & U48 for o in const_values(bv, ssa, disp_expr)}
            offsets = _valid_offsets(bv, slot, table_base_key, key, offsets)
            if offsets:
                return (slot, table_base_key, key & U48, offsets)

    cand = peel_reg_definition(ssa, addr.right)
    if cand is not None and cand.operation.name == "LLIL_ADD":
        disp_expr, tb = addr.left, cand
    else:
        tb = peel_reg_definition(ssa, addr.left)
        disp_expr = addr.right
    if tb is None or tb.operation.name != "LLIL_ADD":
        return None
    table_base_key = _single_const(bv, ssa, disp_expr)
    if table_base_key is None:
        return None

    # rax = OFFSET + [&SLOT] -- table-base add. The [&SLOT] load (of a const
    # pointer) can be on either side; the other operand is the entry offset.
    tb_left = peel_reg_definition(ssa, tb.left)
    tb_right = peel_reg_definition(ssa, tb.right)
    if tb_right is not None and tb_right.operation.name in LOAD_OPS:
        sload, off_expr = tb_right, tb.left
    else:
        sload, off_expr = tb_left, tb.right
    if sload.operation.name not in LOAD_OPS:
        return None
    sp = sload.src
    if sp.operation.name in ("LLIL_CONST_PTR", "LLIL_CONST"):
        slot = sp.constant & U48
    else:
        slot = _single_const(bv, ssa, sp)
    if slot is None:
        return None
    offsets = const_values(bv, ssa, off_expr)
    if not offsets:
        return None

    return (slot, table_base_key & U48, key & U48, {o & U48 for o in offsets})


def parse_jump_gadget_targets(bv, ssa, jump_il):
    """Return every decoded table target tuple for this branch gadget."""
    parsed = _jump_parts(bv, ssa, jump_il)
    if parsed is None:
        return None
    slot, table_base_key, key, offsets = parsed
    return [(slot, table_base_key, key, offset) for offset in sorted(offsets)]


def resolve_llil_jump_targets(bv, ssa, jump_il):
    """Decode every concrete target for one decode-gadget branch."""
    parsed = parse_jump_gadget_targets(bv, ssa, jump_il)
    if parsed is None:
        log_warn(f"[gadget-llil] shape mismatch @ {hex(jump_il.address)}")
        return []
    targets = []
    for slot, table_base_key, key, offset in parsed:
        target = resolve_indirect_jump_addr(bv, slot, offset, table_base_key, key)
        log_debug(f"[gadget-llil] {hex(jump_il.address)} slot={hex(slot)} table_base_key={hex(table_base_key)} "
                  f"key={hex(key)} off={hex(offset)} -> "
                  f"{hex(target) if target is not None else None}")
        if target is not None:
            targets.append(target)
    return sorted(set(targets))


def resolve_llil_jump_plan(bv, llil, known_targets=None):
    """Resolve decode-gadget branches to a plan without mutating BN state."""
    if not llil:
        return []
    if known_targets is None:
        known_targets = {}

    # Phase 1: resolve every target read-only against the original SSA. The
    # decode gadgets are independent, so none of these reads observe a later
    # rewrite -- batching avoids rebuilding SSA between jumps.
    ssa = llil.ssa_form
    pending = []
    for jump_il in iter_llil_indirect_jumps(llil):
        try:
            newly_resolved = jump_il.address not in known_targets
            if jump_il.address in known_targets:
                cached = known_targets[jump_il.address]
                if isinstance(cached, (list, tuple, set)):
                    targets = list(cached)
                else:
                    targets = [cached]
            else:
                # Otherwise resolve it
                targets = resolve_llil_jump_targets(bv, ssa, jump_il)
            targets = [t for t in targets if t is not None and bv.is_valid_offset(t)]
            if not targets:
                continue
            pending.append(branch_fact(
                jump_il.address,
                jump_il.dest.expr_index,
                targets,
                newly_resolved=newly_resolved,
            ))
        except Exception as e:  # noqa: BLE001
            log_error(f"[gadget-llil] {hex(jump_il.address)}: {e}")
            continue

    return pending


def apply_llil_jump_rewrites(bv, llil, plan):
    """Apply current-LLIL rewrites from a branch plan. Does not set user branches."""
    if not llil or not plan:
        return 0

    applied = 0
    for item in plan:
        jump_addr = item["source"]
        targets = item["targets"]
        try:
            if len(targets) == 1:
                new_dest = llil.const_pointer(bv.arch.address_size, targets[0])
                llil.replace_expr(item["dest_expr_index"], new_dest)
                applied += 1
            log_debug(
                f"[gadget-llil] {hex(jump_addr)} -> "
                f"{', '.join(hex(t) for t in targets)}"
            )
        except Exception as e:  # noqa: BLE001
            log_error(f"[gadget-llil] {hex(jump_addr)}: {e}")
            continue

    if applied:
        llil.finalize()
        llil.generate_ssa_form()
    return applied
