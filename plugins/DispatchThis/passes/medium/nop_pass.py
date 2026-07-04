"""Gadget cleanup: convert resolved gadget jumps to gotos and erase dead decode gadgets.

Identifies gadget instructions via signature constants (large non-address decode keys),
grows a taint set by fixpoint, then: converts single-target MLIL_JUMP_TO to goto,
collapses opaque-predicate diamonds, and NOPs tainted pure assignments.
"""

from collections import Counter

from binaryninja import ILSourceLocation, MediumLevelILLabel

from ...utils.log import log_info, log_warn, log_debug


_PURE_ASSIGN_OPS = ("MLIL_SET_VAR_SSA", "MLIL_SET_VAR_SSA_FIELD")
_PHI_OPS = ("MLIL_VAR_PHI",)
_TAINTABLE_OPS = _PURE_ASSIGN_OPS + _PHI_OPS
_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")
_CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")

# A decode key is a constant too wide to be a plausible real immediate: > 32 bits.
# Table-slot pointers are found separately (as load addresses), so this only needs
# to catch the 64-bit keys and never the 32-bit state constants.
_KEY_MIN = 0xFFFFFFFF
_MAGIC_MIN_USES = 3
_U64 = 0xFFFFFFFFFFFFFFFF


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _label(idx):
    lbl = MediumLevelILLabel()
    lbl.operand = idx
    return lbl


def _txt(instr):
    return str(instr).strip()


def _ref_consts(instr):
    """Every constant value referenced anywhere in ``instr``.

    Include the legacy low-32-bit form as well as the full value because v2
    deflatten records state tokens without assuming a 32-bit width.
    """
    values = set()
    for expr in _walk(instr):
        if expr.operation.name not in _CONST_OPS:
            continue
        values.add(expr.constant)
        values.add(expr.constant & 0xFFFFFFFF)
    return values


def _walk(expr):
    """All sub-expression nodes of ``expr`` (inclusive). ``traverse`` returns a
    lazy generator of ``cb(node)`` results, so it MUST be consumed -- using it only
    for side effects silently does nothing."""
    return list(expr.traverse(lambda x: x))


def _contains_op(expr, opnames):
    return any(e.operation.name in opnames for e in _walk(expr))


def _single_target(jump_il):
    """Resolved target instruction index of an ``MLIL_JUMP_TO`` (read from its
    ``targets`` map, not block edges -- those are empty for these jumps), or None
    if it doesn't resolve to exactly one."""
    try:
        tgts = set(jump_il.targets.values())
    except Exception:  # noqa: BLE001
        return None
    return tgts.pop() if len(tgts) == 1 else None


def iter_resolved_gadget_jumps(mlil):
    """Every ``MLIL_JUMP_TO`` with a computed (non-const) dest resolving to one
    successor."""
    for bb in mlil.basic_blocks:
        last = mlil[bb.end - 1]
        if last.operation.name != "MLIL_JUMP_TO":
            continue
        if last.dest.operation.name in _CONST_OPS:
            continue
        if _single_target(last) is None:
            continue
        yield last


# --------------------------------------------------------------------------- #
# gadget signature + taint
# --------------------------------------------------------------------------- #

def gadget_magics(mlil):
    """Return the obfuscator's decode keys: non-address constants > 2^32 appearing >= _MAGIC_MIN_USES times."""
    bv = mlil.source_function.view
    big_consts = Counter()
    nonaddr = Counter()

    n_ins = n_nodes = 0
    for ins in mlil.instructions:
        n_ins += 1
        for e in _walk(ins):
            n_nodes += 1
            if e.operation.name in _CONST_OPS and abs(e.constant) > _KEY_MIN:
                big_consts[e.constant] += 1
                if not bv.is_valid_offset(e.constant & 0xFFFFFFFFFFFFFFFF):
                    nonaddr[e.constant] += 1

    keys = {c for c, n in nonaddr.items() if n >= _MAGIC_MIN_USES}
    # Trace stage counts so an empty result is self-explaining (0 nodes -> traversal
    # broken; big consts seen but all valid addresses -> they were pointers).
    log_info(
        f"[cleanup] gadget_magics: {n_ins} instrs / {n_nodes} nodes scanned; "
        f"{len(big_consts)} distinct >32b const(s), {len(nonaddr)} non-address; "
        f"-> {len(keys)} key(s) {[hex(c) for c in sorted(keys)]}"
    )
    return keys


def _refs_magic(instr, magics):
    return any(e.operation.name in _CONST_OPS and e.constant in magics for e in _walk(instr))


def _ssa_def(ssa, v):
    """SSA definition of ``v``, or None. ``vars_read``/``vars_written`` on SSA
    instructions can include plain (aliased / stack) Variables with no SSA form;
    those raise, and are never gadget temps, so skip them."""
    if not hasattr(v, "version"):
        return None
    try:
        return ssa.get_ssa_var_definition(v)
    except Exception:  # noqa: BLE001
        return None


def _ssa_uses(ssa, w):
    if not hasattr(w, "version"):
        return []
    try:
        return ssa.get_ssa_var_uses(w)
    except Exception:  # noqa: BLE001
        return []


def _defines_in(ssa, v, idxs):
    d = _ssa_def(ssa, v)
    return d is not None and d.instr_index in idxs


def taint_gadget(ssa, magics):
    """Instruction indices of the decode gadget: every pure assign / phi that
    references a magic constant (seed), plus the forward closure (reads a
    gadget-defined variable) and backward closure (all uses are in the gadget --
    pulls in the opaque-predicate offset constants)."""
    gadget = set()
    for ins in ssa.instructions:
        if ins.operation.name in _TAINTABLE_OPS and _refs_magic(ins, magics):
            gadget.add(ins.instr_index)
    seed_count = len(gadget)

    changed = True
    while changed:
        changed = False
        for ins in ssa.instructions:
            idx = ins.instr_index
            if idx in gadget or ins.operation.name not in _TAINTABLE_OPS:
                continue
            forward = any(
                (d := _ssa_def(ssa, v)) is not None and d.instr_index in gadget
                for v in ins.vars_read
            )
            uses = [u for w in ins.vars_written for u in _ssa_uses(ssa, w)]
            backward = bool(uses) and all(u.instr_index in gadget for u in uses)
            if forward or backward:
                gadget.add(idx)
                changed = True
    log_info(f"[cleanup] taint_gadget: {seed_count} seed(s) -> {len(gadget)} tainted instr(s)")
    return gadget


def _decode_root(bv, ins, magics):
    """True if ``ins`` is a pure assign sourced from a gadget decode origin (const-addr load, decode key, or resolved pointer)."""
    if ins.operation.name not in _PURE_ASSIGN_OPS:
        return False
    src = ins.src
    if src.operation.name in _LOAD_OPS:
        return src.src.operation.name in _CONST_OPS
    if src.operation.name in _CONST_OPS:
        c = src.constant
        return c in magics or abs(c) > _KEY_MIN or bv.is_valid_offset(c & _U64)
    return False


def _dead_decode_residue(ssa, magics):
    """Decode residue unreachable by magic taint (zero-use loads, lone keys, folded pointers) minus anything escaping to a live consumer."""
    bv = ssa.source_function.view
    chain = {i.instr_index for i in ssa.instructions if _decode_root(bv, i, magics)}
    seeds = len(chain)

    grew = True
    while grew:                                   # forward-close through pure copies
        grew = False
        for ins in ssa.instructions:
            if ins.instr_index in chain or ins.operation.name not in _TAINTABLE_OPS:
                continue
            if any(_defines_in(ssa, v, chain) for v in ins.vars_read):
                chain.add(ins.instr_index)
                grew = True

    shrank = True
    while shrank:                                 # drop members that escape the slice
        shrank = False
        for idx in list(chain):
            if any(u.instr_index not in chain
                   for w in ssa[idx].vars_written for u in _ssa_uses(ssa, w)):
                chain.discard(idx)
                shrank = True

    log_info(f"[cleanup] dead-decode residue: {seeds} seed(s) -> {len(chain)} instr(s)")
    return chain


def _cond_reads_gadget(ssa, if_il, gadget):
    """True if the ``if``'s condition reads a gadget-tainted variable (an opaque
    predicate)."""
    for v in if_il.ssa_form.vars_read:
        d = _ssa_def(ssa, v)
        if d is not None and d.instr_index in gadget:
            return True
    return False


def _diamond_join(mlil, if_il):
    """The single join both branches of ``if_il`` reconverge at, to ``goto`` when
    collapsing an opaque-predicate diamond, or None if it isn't a clean diamond.
    Handles "default-before-if; then <alt>; both branches -> common join" and
    "one branch falls straight into the other"."""
    t_idx, f_idx = if_il.true, if_il.false
    t_bb = mlil[t_idx].il_basic_block
    f_bb = mlil[f_idx].il_basic_block
    t_succ = [e.target.start for e in t_bb.outgoing_edges]
    f_succ = [e.target.start for e in f_bb.outgoing_edges]
    if len(t_succ) == 1 and len(f_succ) == 1 and t_succ[0] == f_succ[0]:
        return t_succ[0]                      # both branches -> common join
    if len(t_succ) == 1 and t_succ[0] == f_bb.start:
        return f_idx                          # then-branch falls into else (=join)
    if len(f_succ) == 1 and f_succ[0] == t_bb.start:
        return t_idx                          # else-branch falls into then (=join)
    return None



def cleanup_indirect_jumps_opaque_preds(mlil, magics, state_consts=frozenset()):
    """Convert resolved MLIL_JUMP_TO terminators to gotos and collapse opaque-predicate diamonds."""
    ssa = mlil.ssa_form
    gadget = taint_gadget(ssa, magics)

    jump_rewrites = [(j, _single_target(j)) for j in iter_resolved_gadget_jumps(mlil)]

    if_rewrites = []
    for bb in mlil.basic_blocks:
        last = mlil[bb.end - 1]
        if last.operation.name != "MLIL_IF":
            continue
        if not _cond_reads_gadget(ssa, last, gadget):
            continue
        join = _diamond_join(mlil, last)
        if join is None:
            log_warn(f"[cleanup] opaque predicate at: {hex(last.address)} has no clean join")
            continue
        if_rewrites.append((last, join))

    dead = _dead_decode_residue(ssa, magics)
    nop_nz = {}
    nop_src = {}
    for idx in gadget | dead:
        ins = ssa[idx]
        if ins.operation.name not in _PURE_ASSIGN_OPS:
            continue  # phis carry no non-SSA instruction; nothing to NOP
        nz = ins.non_ssa_form
        if nz is not None:
            nop_nz[nz.instr_index] = nz
            nop_src[nz.instr_index] = "gadget-taint" if idx in gadget else "dead-decode"

    if not jump_rewrites and not if_rewrites and not nop_nz:
        return 0, 0, 0

    done = set()
    #for jump_il, target in jump_rewrites:
    #    mlil.replace_expr(
    #        jump_il.expr_index, mlil.goto(_label(target), ILSourceLocation.from_instruction(jump_il))
    #    )
    #    log_debug(f"[cleanup] jump -> goto {hex(mlil[target].address)} @ {hex(jump_il.address)}")

    # Collapse opaque ifs BEFORE NOPing, so each if stops reading its (about to be
    # NOP'd) condition variable.
    for if_il, join in if_rewrites:
        if if_il.instr_index in done:
            continue
        done.add(if_il.instr_index)
        log_debug(f"[cleanup] collapse opaque if @ {hex(if_il.address)} -> goto {hex(mlil[join].address)}")
        mlil.replace_expr(
            if_il.expr_index, mlil.goto(_label(join), ILSourceLocation.from_instruction(if_il))
        )

    for idx, nz in nop_nz.items():
        if idx in done:
            continue
        done.add(idx)
        hit = _ref_consts(nz) & state_consts
        mlil.replace_expr(nz.expr_index, mlil.nop(ILSourceLocation.from_instruction(nz)))

        if hit:
            log_info(
                f"[cleanup] NOP'd STATE-CONST write @ {hex(nz.address)} "
                f"(const {', '.join(hex(c) for c in sorted(hit))}, via {nop_src.get(idx)}): {_txt(nz)}"
            )

    jump_rewrites_len = len(jump_rewrites)
    if_rewrites_len = len(if_rewrites)
    nop_nz_len = len(nop_nz)

    if jump_rewrites_len or if_rewrites_len or nop_nz_len:
        mlil.finalize()
        mlil.generate_ssa_form()
        log_info(
            f"[cleanup] {jump_rewrites_len} jump->goto, {if_rewrites_len} opaque-if collapse(s), {nop_nz_len} gadget NOP(s)"
        )
    return jump_rewrites_len, if_rewrites_len, nop_nz_len


def nop_state_writes(mlil, state_consts, state_vars):
    """NOP the flattener's state writes matched by state constant value and/or state-variable alias.
    Returns the count."""
    state_consts = state_consts or set()
    state_vars = state_vars or set()
    if not state_consts and not state_vars:
        log_info("[cleanup] state-writes: nothing recorded by deflatten; skipping")
        return 0

    u32 = 0xFFFFFFFF
    var_ops = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")
    store_ops = ("MLIL_STORE", "MLIL_STORE_STRUCT")
    seen = set()
    for ins in mlil.instructions:
        if ins.operation.name not in var_ops + store_ops:
            continue
        if ins.instr_index in seen:
            continue
        by_value = ins.src.operation.name in _CONST_OPS and (
            ins.src.constant in state_consts or (ins.src.constant & u32) in state_consts
        )
        by_var = ins.operation.name in var_ops and ins.dest in state_vars
        if not (by_value or by_var):
            continue
        seen.add(ins.instr_index)
        reason = []
        if by_value:
            reason.append(f"value={hex(ins.src.constant & u32)}")
        if by_var:
            reason.append(f"dest={ins.dest}")
        log_info(
            f"[cleanup] state-write NOP @ {hex(ins.address)} ({', '.join(reason)}): {_txt(ins)}"
        )
        mlil.replace_expr(ins.expr_index, mlil.nop(ILSourceLocation.from_instruction(ins)))
    if seen:
        mlil.finalize()
        mlil.generate_ssa_form()
    log_info(
        f"[cleanup] state-writes: NOP'd {len(seen)} write(s) "
        f"({len(state_consts)} state const(s), {len(state_vars)} state var/alias(es))"
    )
    return len(seen)


def clean_resolved_gadget_jumps(bv, func):
    """Convert resolved gadget jumps to gotos and erase their decode gadgets by
    signature taint, in rounds to a fixpoint, then NOP the recorded state writes.
    Returns gadget jumps converted."""
    if func.medium_level_il is None:
        return 0, 0, 0, 0

    magics = gadget_magics(func.medium_level_il)
    log_info(
        f"[cleanup] BUILD=signature-v6 starting on {func.name}; "
        f"magics={{{', '.join(hex(m) for m in sorted(magics))}}}"
    )
    if not magics:
        log_warn(f"[cleanup] {func.name}: no gadget signature constants found")

    state_consts = bv.session_data.get("dispatchthis_state_consts", {}).get(func.start, set())
    state_vars = bv.session_data.get("dispatchthis_state_vars", {}).get(func.start, set())

    jumps, ifs, nops = cleanup_indirect_jumps_opaque_preds(func.medium_level_il, magics, state_consts)

    nopd_state_writes = nop_state_writes(func.medium_level_il, state_consts, state_vars)

    log_info(f"[cleanup] {func.name}: NOP'd {nopd_state_writes} instructions")

    return jumps, ifs, nops, nopd_state_writes
