"""
LLIL-stage jump gadget resolver that replaces indirect jump expressions
with replace_expr and an associated jump to the original address 
"""

from collections import defaultdict

from binaryninja import LowLevelILOperation as L

from ...helpers.facts import branch_fact
from ...helpers.llil import (
    CONST_OPERATIONS,
    LOAD_OPERATIONS,
    U48,
    const_values,
    iter_indirect_jumps as iter_llil_indirect_jumps,
    peel_reg_definition,
)
from ...helpers.memory import is_executable_target, read_u64le
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
    return next(iter(values)) if values is not None and len(values) == 1 else None


def _slot_from_load(bv, ssa, sload):
    if sload is None or sload.operation not in LOAD_OPERATIONS:
        return None
    sp = sload.src
    if sp.operation in CONST_OPERATIONS:
        return sp.constant & U48
    return _single_const(bv, ssa, sp)


def _slot_add_const_candidates(bv, ssa, expr):
    expr = peel_reg_definition(ssa, expr)
    if expr is None or expr.operation != L.LLIL_ADD:
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
    normalized = {offset & U48 for offset in offsets}
    if not normalized:
        return None
    for offset in normalized:
        target = resolve_indirect_jump_addr(bv, slot, offset, table_base_key, key)
        if target is None or not is_executable_target(bv, target):
            return None
    return normalized


def _consensus_jump_parts(candidates):
    unique = {}
    for slot, table_base_key, key, offsets in candidates:
        semantic_key = (slot, table_base_key, key, frozenset(offsets))
        unique.setdefault(semantic_key, (slot, table_base_key, key, set(offsets)))
    return next(iter(unique.values())) if len(unique) == 1 else None


def _is_index_offset(ssa, expr):
    expr = peel_reg_definition(ssa, expr)
    return expr is not None and expr.operation in (L.LLIL_LSL, L.LLIL_LSR)


def _jump_parts(bv, ssa, jump_il):
    """
    
    Walk the decode gadget feeding ``jump_il`` backwards through LLIL SSA.

    Returns ``(slot, table_base_key, key, offsets)`` as ints, or ``None`` if the
    gadget does not match the expected shape."""

    jdest = jump_il.ssa_form.dest
    if jdest.operation != L.LLIL_REG_SSA:
        return None

    # rax = rax (+/-) KEY -- the final decode step is `add` or `sub`; for `sub`
    # the effective key is negated (the U48 decode math treats it as addition).
    fin = peel_reg_definition(ssa, jdest)
    if fin is None or fin.operation not in (L.LLIL_ADD, L.LLIL_SUB):
        return None
    if fin.operation == L.LLIL_SUB:
        chain_expr, key_expr, neg = fin.left, fin.right, True
    else:
        # commutative: the chain operand leads to the entry LOAD; other is key.
        if peel_reg_definition(ssa, fin.left).operation in LOAD_OPERATIONS:
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
    if load is None or load.operation not in LOAD_OPERATIONS:
        return None
    addr = load.src
    if addr.operation != L.LLIL_ADD:
        return None

    candidates = []

    # Some ARM64 samples pre-add the table-base key before indexing:
    #     target = *(*slot + table_base_key + offset) + key
    for tb, disp_expr in (
        (peel_reg_definition(ssa, addr.left), addr.right),
        (peel_reg_definition(ssa, addr.right), addr.left),
    ):
        if not _is_index_offset(ssa, disp_expr):
            continue
        for slot, table_base_key in _slot_add_const_candidates(bv, ssa, tb):
            offset_values = const_values(bv, ssa, disp_expr)
            if offset_values is None:
                continue
            offsets = _valid_offsets(
                bv,
                slot,
                table_base_key,
                key,
                offset_values,
            )
            if offsets is not None:
                candidates.append((slot, table_base_key, key & U48, offsets))

    # rax = OFFSET + [&SLOT] -- enumerate both ADD orientations and both
    # possible slot-load operands. Multiple successful interpretations must
    # agree semantically; operand order never chooses a winner.
    for table_base_expr, disp_expr in (
        (peel_reg_definition(ssa, addr.right), addr.left),
        (peel_reg_definition(ssa, addr.left), addr.right),
    ):
        if table_base_expr is None or table_base_expr.operation != L.LLIL_ADD:
            continue
        table_base_key = _single_const(bv, ssa, disp_expr)
        if table_base_key is None:
            continue
        for slot_load, offset_expr in (
            (peel_reg_definition(ssa, table_base_expr.right), table_base_expr.left),
            (peel_reg_definition(ssa, table_base_expr.left), table_base_expr.right),
        ):
            slot = _slot_from_load(bv, ssa, slot_load)
            if slot is None:
                continue
            offset_values = const_values(bv, ssa, offset_expr)
            if offset_values is None:
                continue
            offsets = _valid_offsets(
                bv,
                slot,
                table_base_key,
                key,
                offset_values,
            )
            if offsets is not None:
                candidates.append((slot, table_base_key & U48, key & U48, offsets))

    return _consensus_jump_parts(candidates)


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
        log_debug(f"[gadget-llil] shape mismatch @ {hex(jump_il.address)}")
        return []
    targets = []
    for slot, table_base_key, key, offset in parsed:
        target = resolve_indirect_jump_addr(bv, slot, offset, table_base_key, key)
        log_debug(f"[gadget-llil] {hex(jump_il.address)} slot={hex(slot)} table_base_key={hex(table_base_key)} "
                  f"key={hex(key)} off={hex(offset)} -> "
                  f"{hex(target) if target is not None else None}")
        if target is None or not is_executable_target(bv, target):
            return []
        targets.append(target)
    return sorted(set(targets))


def _validated_targets(bv, targets):
    targets = list(targets or ())
    if not targets or any(
        type(target) is not int or not is_executable_target(bv, target)
        for target in targets
    ):
        return None
    return sorted(set(targets))


def _current_jump_groups(llil):
    groups = defaultdict(list)
    for jump in iter_llil_indirect_jumps(llil):
        source = getattr(jump, "address", None)
        dest_index = getattr(getattr(jump, "dest", None), "expr_index", None)
        if type(source) is int and source >= 0 and type(dest_index) is int and dest_index >= 0:
            groups[source].append(jump)
    return groups


def _same_jump_witness(candidate, current, llil):
    if (
        candidate is None
        or getattr(candidate, "operation", None) != getattr(current, "operation", None)
        or getattr(candidate, "address", None) != getattr(current, "address", None)
        or getattr(getattr(candidate, "dest", None), "expr_index", None)
        != getattr(getattr(current, "dest", None), "expr_index", None)
    ):
        return False
    for attr in ("instr_index", "expr_index"):
        recorded = getattr(candidate, attr, None)
        actual = getattr(current, attr, None)
        if type(recorded) is not int or type(actual) is not int or recorded != actual:
            return False
    recorded_owner = getattr(candidate, "function", None)
    current_owner = getattr(current, "function", None)
    if recorded_owner is not None or current_owner is not None:
        return recorded_owner is llil and current_owner is llil
    # Lightweight test doubles do not model the owning IL function. Real BNIL
    # instructions do, and the branch above requires that exact current owner.
    return True


def validate_current_branch_plans(bv, llil, plan):
    """Keep only complete, non-conflicting facts witnessed by current LLIL."""
    current_groups = _current_jump_groups(llil)
    groups = defaultdict(list)
    for item in plan or ():
        source = item.get("source")
        if type(source) is int and source >= 0:
            groups[source].append(item)

    accepted = []
    for source, items in groups.items():
        try:
            semantics = set()
            for item in items:
                targets = list(item.get("targets", ()))
                if (
                    not targets
                    or any(type(target) is not int or target < 0 for target in targets)
                    or (
                        hasattr(bv, "is_offset_executable")
                        and _validated_targets(bv, targets) is None
                    )
                ):
                    raise ValueError("invalid targets")
                semantics.add((item.get("dest_expr_index"), tuple(sorted(set(targets)))))
        except (AttributeError, TypeError, ValueError):
            log_warn(f"[gadget-llil] malformed branch plan @ {source:#x}")
            continue
        current = current_groups.get(source, ())
        if len(semantics) != 1 or len(current) != 1:
            log_warn(f"[gadget-llil] conflicting or stale branch plan @ {source:#x}")
            continue
        dest_index, _targets = next(iter(semantics))
        if (
            type(dest_index) is not int
            or dest_index < 0
            or current[0].dest.expr_index != dest_index
            or any(
                not _same_jump_witness(item.get("jump_il"), current[0], llil)
                for item in items
            )
        ):
            log_warn(f"[gadget-llil] rejected stale jump witness @ {source:#x}")
            continue
        accepted.append(items[0])
    return accepted


def resolve_llil_jump_plan(bv, llil, known_targets=None):
    """Resolve the current branch frontier without mutating BN state.

    ``known_targets`` contains receipts already verified against Binary Ninja's
    current user branch metadata. Their sources are therefore outside this
    run's decode frontier.
    """
    if not llil:
        return []

    # Phase 1: resolve every target read-only against the original SSA. The
    # decode gadgets are independent, so none of these reads observe a later
    # rewrite -- batching avoids rebuilding SSA between jumps.
    ssa = llil.ssa_form
    pending = []
    grouped = _current_jump_groups(llil)
    for source, jumps in grouped.items():
        if source in (known_targets or {}):
            continue
        facts = []
        for jump_il in jumps:
            try:
                targets = resolve_llil_jump_targets(bv, ssa, jump_il)
                targets = _validated_targets(bv, targets)
                if targets is None:
                    facts = []
                    break
                facts.append(branch_fact(
                    jump_il,
                    targets,
                ))
            except Exception as e:  # noqa: BLE001
                log_error(f"[gadget-llil] {source:#x}: {e}")
                facts = []
                break

        semantics = {
            (fact["dest_expr_index"], fact["targets"])
            for fact in facts
        }
        if len(semantics) == 1:
            pending.append(facts[0])
        elif facts:
            log_warn(f"[gadget-llil] conflicting branch facts @ {source:#x}")

    return pending


def apply_llil_jump_rewrites(bv, llil, plan):
    """Apply current-LLIL rewrites from a branch plan. Does not set user branches."""
    if not llil or not plan:
        return 0

    plan = validate_current_branch_plans(bv, llil, plan)

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
