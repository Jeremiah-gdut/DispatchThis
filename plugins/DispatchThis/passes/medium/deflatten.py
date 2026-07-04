"""Deflattening core: turn ``OBB -> dispatcher`` edges into ``OBB -> next OBB``.

``compute_redirections`` -- read-only analysis recovering dispatcher state-token
transitions to determine which jumps/branches to re-point.
``apply_redirections_il`` -- rewrites MLIL in place; only meaningful inside a workflow activity.
"""

from binaryninja import (
    ILSourceLocation,
    MediumLevelILJump,
    MediumLevelILLabel,
)

from ...utils.state_machine import resolve_to_constants, match_successor

U32 = 0xFFFFFFFF
from ...utils.log import log_info, log_warn, log_debug
from collections import deque


def _mask(size):
    return (1 << ((size or 4) * 8)) - 1


def _op(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def _last(mlil, bb):
    return mlil[bb.end - 1]


def _block_at(mlil, instr_index):
    return mlil[instr_index].il_basic_block


def _state_token(const_expr, fallback_size=None):
    size = getattr(const_expr, "size", None) or fallback_size
    if size is None:
        value = getattr(const_expr, "constant", 0)
        size = 8 if value > U32 or value < 0 else 4
    return (const_expr.constant & _mask(size), size)


def _same_var(left, right):
    return left == right or str(left) == str(right)


def _var_from_expr(expr):
    op = _op(expr)
    if op in ("MLIL_VAR", "MLIL_VAR_FIELD"):
        return expr.src
    if op in ("MLIL_VAR_SSA", "MLIL_VAR_FIELD_SSA"):
        return getattr(expr.src, "var", expr.src)
    return None


def _resolve_cond(cond):
    if cond.operation.name != "MLIL_VAR":
        return cond

    try:
        ssa_cond = cond.ssa_form          # MLIL_VAR_SSA expression
        ssa_var  = ssa_cond.src           # SSAVariable
        def_site = cond.function.ssa_form.get_ssa_var_definition(ssa_var)
        if def_site is not None:
            cond = def_site.src           # RHS of the defining MLIL_SET_VAR_SSA
    except AttributeError:
        return cond

    return cond


def _cmp_parts(cond, equality_only=False):
    cond = _resolve_cond(cond)
    op = _op(cond)
    if not (op or "").startswith("MLIL_CMP"):
        return None
    if equality_only and op != "MLIL_CMP_E":
        return None
    sides = (cond.left, cond.right)
    var_expr = next((s for s in sides if _var_from_expr(s) is not None), None)
    const_expr = next((s for s in sides if _op(s) == "MLIL_CONST"), None)
    if var_expr is None or const_expr is None:
        return None
    return _var_from_expr(var_expr), _state_token(const_expr)


def _cmp_e_parts(cond):
    return _cmp_parts(cond, equality_only=True)


def _trace_var_roots(mlil, var, seen=None, depth=0):
    if seen is None:
        seen = set()
    if var in seen or depth > 12:
        return set()
    seen.add(var)

    defs = list(mlil.get_var_definitions(var))
    if not defs:
        return {var}
    if any(_var_from_expr(getattr(definition, "src", None)) is None for definition in defs):
        return {var}

    roots = set()
    for definition in defs:
        src = getattr(definition, "src", None)
        roots.update(_trace_var_roots(mlil, _var_from_expr(src), seen, depth + 1))
    return roots


def _dispatcher_rows(mlil):
    rows = []
    for bb in mlil.basic_blocks:
        last = _last(mlil, bb)
        if _op(last) != "MLIL_IF":
            continue
        parts = _cmp_e_parts(last.condition)
        if parts is None:
            continue
        var, token = parts
        roots = _trace_var_roots(mlil, var)
        if len(roots) != 1:
            continue
        rows.append(
            {
                "bb": bb,
                "if_il": last,
                "var": var,
                "root": next(iter(roots)),
                "token": token,
            }
        )
    return rows


def _router_boundary_block(mlil, bb, state_var=None):
    if state_var is not None and any(
        _op(ins) == "MLIL_SET_VAR" and _same_var(getattr(ins, "dest", None), state_var)
        for ins in bb
    ):
        return False
    last = _last(mlil, bb)
    if _op(last) == "MLIL_IF":
        parts = _cmp_parts(last.condition)
        if parts is None:
            return False
        if state_var is None:
            return True
        var, _token = parts
        return any(_same_var(root, state_var) for root in _trace_var_roots(mlil, var))
    return all(_op(ins) in {"MLIL_SET_VAR", "MLIL_GOTO", "MLIL_NOP"} for ins in bb)


def _expand_dispatcher_boundary(mlil, starts, state_var):
    expanded = set(starts)
    by_start = {bb.start: bb for bb in mlil.basic_blocks}
    queue = deque(by_start[start] for start in starts if start in by_start)
    while queue:
        bb = queue.popleft()
        for edge in bb.incoming_edges:
            pred = edge.source
            if pred.start in expanded or not _router_boundary_block(mlil, pred, state_var):
                continue
            expanded.add(pred.start)
            if len(pred.incoming_edges) <= 2:
                queue.append(pred)
    return expanded


def _analyze_dispatcher(mlil):
    rows = _dispatcher_rows(mlil)
    if len(rows) < 3:
        return None

    all_rows = rows
    groups = {}
    for row in rows:
        key = (str(row["root"]), row["token"][1])
        groups.setdefault(key, []).append(row)
    candidate_groups = [group for group in groups.values() if len(group) >= 3]
    if not candidate_groups:
        return None
    candidate_groups.sort(key=len, reverse=True)
    if len(candidate_groups) > 1:
        log_warn("[deflat] dispatcher cluster has ambiguous state roots; skipping")
        return None
    rows = candidate_groups[0]
    ignored = len(all_rows) - len(rows)
    if ignored:
        log_debug(f"[deflat] ignoring {ignored} non-dominant dispatcher row(s)")

    roots = {row["root"] for row in rows}
    if len(roots) != 1:
        log_warn("[deflat] dispatcher cluster has multiple state roots; skipping")
        return None
    sizes = {row["token"][1] for row in rows}

    token_targets = {}
    for row in rows:
        token_targets[row["token"]] = _block_at(mlil, row["if_il"].true)

    dispatcher_starts = {row["bb"].start for row in rows}
    for bb in mlil.basic_blocks:
        last = _last(mlil, bb)
        if _op(last) != "MLIL_IF":
            continue
        parts = _cmp_parts(last.condition)
        if parts is None:
            continue
        var, _token = parts
        if any(_same_var(root, next(iter(roots))) for root in _trace_var_roots(mlil, var)):
            dispatcher_starts.add(bb.start)
    dispatcher_starts = _expand_dispatcher_boundary(mlil, dispatcher_starts, next(iter(roots)))

    return {
        "state_var": next(iter(roots)),
        "token_size": next(iter(sizes)),
        "dispatcher_starts": dispatcher_starts,
        "token_targets": token_targets,
        "state_tokens": set(token_targets),
    }


def _region_until(mlil, start_bb, stop_starts, state_var=None, state_tokens=None):
    region = set()
    queue = deque([start_bb])
    while queue:
        bb = queue.popleft()
        if bb.start in region or bb.start in stop_starts:
            continue
        if bb.start != start_bb.start and state_var is not None and _op(_last(mlil, bb)) == "MLIL_IF":
            parts = _cmp_parts(_last(mlil, bb).condition)
            compares_state_token = state_tokens is not None and parts is not None and parts[1] in state_tokens
            if compares_state_token or _router_boundary_block(mlil, bb, state_var):
                continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            if edge.target.start not in region and edge.target.start not in stop_starts:
                queue.append(edge.target)
    return region


def _private_exit(mlil, head, region, stop_starts):
    queue = deque([head])
    seen = set()
    while queue:
        bb = queue.popleft()
        if bb.start in seen:
            continue
        seen.add(bb.start)
        for edge in bb.outgoing_edges:
            succ = edge.target
            if succ.start in stop_starts:
                return _last(mlil, bb)
            if succ.start in region:
                foreign = [
                    e.source for e in succ.incoming_edges
                    if e.source.start not in region and e.source.start not in stop_starts
                ]
                if foreign:
                    return _last(mlil, bb)
                queue.append(succ)
    return None


def _resolve_tokens_from_expr(mlil, expr, token_size, scope, seen=None):
    if seen is None:
        seen = set()
    op = _op(expr)
    if op == "MLIL_CONST":
        return {_state_token(expr, token_size)}
    source_var = _var_from_expr(expr)
    if source_var is None:
        return set()
    if source_var in seen:
        return set()
    seen.add(source_var)
    tokens = set()
    for definition in mlil.get_var_definitions(source_var):
        if definition.il_basic_block.start not in scope:
            continue
        tokens.update(_resolve_tokens_from_expr(mlil, definition.src, token_size, scope, seen))
    return tokens


def _state_write_tokens(mlil, root, token_size, scope):
    tokens = set()
    for bb in mlil.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            if _op(ins) != "MLIL_SET_VAR" or not _same_var(ins.dest, root):
                continue
            tokens.update(_resolve_tokens_from_expr(mlil, ins.src, token_size, scope))
    return tokens


def _selection_vars(mlil, root, scope):
    vars_ = {root}
    for bb in mlil.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            if _op(ins) == "MLIL_SET_VAR":
                vars_.update(getattr(ins, "vars_written", ()) or ())
    return vars_


def _pure_state_selection_tail(mlil, scope):
    allowed = {"MLIL_SET_VAR", "MLIL_IF", "MLIL_GOTO", "MLIL_NOP"}
    for bb in mlil.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            if _op(ins) not in allowed:
                return False
    return True


def _plan_conditional(mlil, head, region, analysis):
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    stop_starts = analysis["dispatcher_starts"]
    token_targets = analysis["token_targets"]
    for bb in mlil.basic_blocks:
        if bb.start not in region:
            continue
        if_il = _last(mlil, bb)
        if _op(if_il) != "MLIL_IF":
            continue
        true_bb = _block_at(mlil, if_il.true)
        false_bb = _block_at(mlil, if_il.false)
        if true_bb.start in stop_starts or false_bb.start in stop_starts:
            continue
        true_scope = _region_until(mlil, true_bb, stop_starts, root, analysis["state_tokens"])
        false_scope = _region_until(mlil, false_bb, stop_starts, root, analysis["state_tokens"])
        if not _pure_state_selection_tail(mlil, true_scope | false_scope):
            continue
        true_tokens = _state_write_tokens(mlil, root, token_size, true_scope)
        false_tokens = _state_write_tokens(mlil, root, token_size, false_scope)
        if len(true_tokens) != 1 or len(false_tokens) != 1:
            continue
        true_token = next(iter(true_tokens))
        false_token = next(iter(false_tokens))
        if true_token not in token_targets or false_token not in token_targets:
            continue
        if true_token == false_token:
            continue
        return {
            "kind": "if_else",
            "obb": head,
            "if_il": if_il,
            "true_target": token_targets[true_token],
            "false_target": token_targets[false_token],
            "true_token": true_token,
            "false_token": false_token,
            "state_var": root,
            "state_vars": _selection_vars(mlil, root, true_scope | false_scope),
            "state_tokens": {true_token, false_token},
        }
    return None


def _plan_head_transition(mlil, head, analysis):
    stop_starts = analysis["dispatcher_starts"]
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    token_targets = analysis["token_targets"]
    region = _region_until(mlil, head, stop_starts, root, analysis["state_tokens"])

    cond_plan = _plan_conditional(mlil, head, region, analysis)
    if cond_plan is not None:
        return cond_plan

    tokens = _state_write_tokens(mlil, root, token_size, region)
    if len(tokens) != 1:
        return None
    token = next(iter(tokens))
    target = token_targets.get(token)
    if target is None:
        return None
    exit_jump = _private_exit(mlil, head, region, stop_starts)
    if exit_jump is None:
        return None
    return {
        "jump": exit_jump,
        "target_bb": target,
        "obb": head,
        "kind": "uncond",
        "state_var": root,
        "state_vars": {root},
        "state_token": token,
        "state_tokens": {token},
    }


def find_prolog_exit_jump_instr(mlil, start_bb):
    """
    Walk the chain until we find the dispatcher, all previous blocks are
    part of the prolog, get the last jump or goto and return that instruction.

    The dispatcher is a MLIL_IF where the left side is a MLIL_CMP_SGT
    and the right side a MLIL_CONST. It has a single incoming edge and two outgoing edges.
    """

    seen = set()
    queue = deque([start_bb])
    while queue:
        bb = queue.popleft()
        if bb.start in seen:
            continue
        seen.add(bb.start)

        for edge in bb.outgoing_edges:
            succ = edge.target
            if _is_dispatcher_block(mlil, succ):
                dispatcher_addr = mlil[succ.start].address
                log_info(
                    f"[deflat] found dispatcher block: {succ.start} ({hex(dispatcher_addr)}), "
                    f"prologue exit: {hex(mlil[bb.end - 1].address)}"
                )
                return mlil[bb.end - 1]
            if succ.start not in seen:
                queue.append(succ)


def _is_dispatcher_block(mlil, bb):
    if len(bb.outgoing_edges) != 2:
        return False
    last = mlil[bb.end - 1]
    if last.operation.name != "MLIL_IF":
        return False
    cond = _resolve_cond(last.condition)
    if getattr(cond, "operation", None) is None:
        return False
    if not cond.operation.name in ("MLIL_CMP_SGT", "MLIL_CMP_SLT", "MLIL_CMP_SGE", "MLIL_CMP_SLE"):
        return False
    if cond.right.operation.name != "MLIL_CONST":
        return False
    if cond.right.size != 4:
        return False
    if cond.right.constant in [0x9, 0xA]:
        return False
    return True


def _prolog_region(mlil):
    """Block-start indices reachable from the function entry *before* the
    dispatcher.

    The prolog's state-write block is not necessarily block 0 -- some prologs
    do their initial state write a few blocks in (after a stack-setup / cmov
    selection). What distinguishes the prolog from an OBB is reachability: the
    prolog flows from the function entry into the dispatcher, whereas every OBB
    is reached only *through* the dispatcher. So we forward-walk from entry and
    stop at the dispatcher; every block we collect belongs to the prolog."""
    region = set()
    queue = deque([mlil.basic_blocks[0]])
    while queue:
        bb = queue.popleft()
        if bb.start in region:
            continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            succ = edge.target
            if _is_dispatcher_block(mlil, succ):
                return region  # stop at the dispatcher; don't cross into the OBBs
            if succ.start not in region:
                queue.append(succ)


def find_chain_exit_jump_instr(mlil, bb, gadget_map):
    """Find the OBB's chain-exit jump: the resolved gadget terminator just before the dispatcher complex."""

    # Walk the chain forward. Chain blocks have <= 2 incoming edges (ordinary
    # gadget-jump joins and if-diamonds reconverging). The dispatcher complex is
    # the first thing with > 2 incoming edges -- so the block that *precedes* it
    # (via its single outgoing edge) is the chain-exit candidate. Diamonds have
    # two outgoing edges that reconverge inside the chain, so we explore all
    # forward edges but never step into a > 2-incoming edge block.
    seen = set()
    stack = [bb]
    while stack:
        bb = stack.pop()
        if bb.start in seen:
            continue
        seen.add(bb.start)

        for edge in bb.outgoing_edges:
            succ = edge.target
            if len(succ.incoming_edges) > 2:
                # `succ` is the dispatcher complex; `bb` precedes it. The exit
                # jump must also match the full chain-exit shape.
                if _is_chain_exit_block(mlil, bb, gadget_map):
                    log_debug(f"[deflat] MLIL_JUMP_TO-based exit jump identified at {hex(mlil[bb.end - 1].address)} => {mlil[bb.end - 1]}")
                    return mlil[bb.end - 1]
                # Some OBBs route back to the dispatcher with a plain
                # `jmp dispatcher` (a direct MLIL_GOTO) instead of an indirect
                # decode gadget
                if _is_direct_goto_exit_block(mlil, bb, succ):
                    log_debug(f"[deflat] MLIL_GOTO-based exit jump identified at {hex(mlil[bb.end - 1].address)} => {mlil[bb.end - 1]}")
                    return mlil[bb.end - 1]
                # Otherwise this is a dead end into the dispatcher; don't follow.
                continue
            if succ.start not in seen:
                stack.append(succ)
    return None


def _is_chain_exit_block(mlil, bb, gadget_map):
    """True if ``bb`` is a chain-exit block: its last instruction is a resolved
    ``MLIL_JUMP_TO`` decode gadget, it has a single outgoing edge, and exactly
    two incoming edges -- one from an ``MLIL_IF`` block and one from an
    ``MLIL_GOTO`` block."""
    last = mlil[bb.end - 1]
    if last.operation.name != "MLIL_JUMP_TO" or last.address not in gadget_map:
        return False
    if len(bb.outgoing_edges) != 1:
        return False
    incoming = bb.incoming_edges
    if len(incoming) != 2:
        return False
    pred_terms = {mlil[e.source.end - 1].operation.name for e in incoming}
    return pred_terms == {"MLIL_IF", "MLIL_GOTO"}


def _is_direct_goto_exit_block(mlil, bb, dispatcher_bb):
    """True if ``bb`` exits via a plain ``MLIL_GOTO`` directly to the dispatcher (no decode-gadget shape)."""
    last = mlil[bb.end - 1]
    if last.operation.name != "MLIL_GOTO":
        return False
    if len(bb.outgoing_edges) != 1:
        return False
    return bb.outgoing_edges[0].target.start == dispatcher_bb.start


def _temp_const(func, il, temp_var, mask):
    """Return the single masked constant ``temp_var`` is assigned in ``il``, or None.
    Resolves transitively through VAR copies, not just literal MLIL_CONST."""
    if il.operation.name != "MLIL_SET_VAR" or il.dest != temp_var:
        return None
    consts = set(resolve_to_constants(func, il))
    if len(consts) != 1:
        return None
    return consts.pop() & mask


def _single_succ(bb):
    outs = bb.outgoing_edges
    return outs[0].target if len(outs) == 1 else None


def _classify_diamond(true_bb, false_bb):
    """A cmov diamond: one successor is the *then* block (assigns the alternate,
    then falls into the join); the other is the *join* (continuation). The then
    block's sole successor IS the join. Returns ``(then_bb, then_is_true, cont_bb)``
    or ``(None, None, None)`` if the shape doesn't match."""
    s = _single_succ(true_bb)
    if s is not None and s.start == false_bb.start:
        return true_bb, True, false_bb
    s = _single_succ(false_bb)
    if s is not None and s.start == true_bb.start:
        return false_bb, False, true_bb
    return None, None, None


def _z3_first_equals_override(diamonds, default_const):
    """True if first-fire routing == cmov override (last-fire) routing for every
    combination of conditions -- i.e. the chain is monotone and can be rewritten
    by local forward edge re-pointing. Otherwise the final state depends on a
    later condition overriding an earlier one and we must branch on the computed
    value instead."""
    import z3

    bs = [z3.Bool(f"b{i}") for i in range(len(diamonds))]
    consts = [d["then_const"] for d in diamonds]

    override = z3.BitVecVal(default_const, 64)  # later fired wins (real semantics)
    for b, c in zip(bs, consts):
        override = z3.If(b, z3.BitVecVal(c, 64), override)

    first = z3.BitVecVal(default_const, 64)  # earlier fired wins (forward routing)
    for b, c in reversed(list(zip(bs, consts))):
        first = z3.If(b, z3.BitVecVal(c, 64), first)

    s = z3.Solver()
    s.add(override != first)
    return s.check() == z3.unsat


def build_cond_plan(mlil, link, jump):
    """Build a rewrite plan for a conditional (cmov-selected) transition. Returns the plan dict or None."""
    func = mlil.source_function
    write_il = link.il
    if write_il.src.operation.name != "MLIL_VAR":
        return None
    temp_var = write_il.src.src
    size = write_il.src.size or 4
    mask = _mask(size)
    converged = link.block
    cases_map = {s & mask: bb for s, bb in link.cases}

    # The default value is assigned in the chain's entry block -- the only temp
    # definition whose block terminates in the first MLIL_IF (alternate values
    # live in then-blocks, which terminate in MLIL_GOTO).
    default_const = None
    entry_block = None
    for d in mlil.get_var_definitions(temp_var):
        bb = d.il_basic_block
        if mlil[bb.end - 1].operation.name != "MLIL_IF":
            continue
        val = _temp_const(func, d, temp_var, mask)
        if val is not None:
            default_const, entry_block = val, bb
            break
    if entry_block is None:
        return None

    # Walk the diamonds forward from the entry to the converged (store) block.
    diamonds = []
    bb = entry_block
    for _ in range(256):
        if_il = mlil[bb.end - 1]
        if if_il.operation.name != "MLIL_IF":
            return None
        true_bb = mlil[if_il.true].il_basic_block
        false_bb = mlil[if_il.false].il_basic_block
        then_bb, then_is_true, cont_bb = _classify_diamond(true_bb, false_bb)
        if then_bb is None:
            return None
        then_const = next(
            (v for ins in then_bb if (v := _temp_const(func, ins, temp_var, mask)) is not None),
            None,
        )
        if then_const is None:
            return None
        diamonds.append(
            {
                "if_il": if_il,
                "then_is_true": then_is_true,
                "then_const": then_const,
                "cont_bb": cont_bb,
            }
        )
        if cont_bb.start == converged.start:
            break
        bb = cont_bb
    else:
        return None

    # Every state constant must resolve to a known successor.
    for c in [default_const, *(d["then_const"] for d in diamonds)]:
        if c not in cases_map:
            return None

    return {
        "kind": "cond",
        "obb": converged,
        "jump": jump,
        "temp_var": temp_var,
        "size": size,
        "mask": mask,
        "default_const": default_const,
        "diamonds": diamonds,
        "cases_map": cases_map,
        "monotone": _z3_first_equals_override(diamonds, default_const),
    }


def _chain_region_blocks(mlil, head):
    """Block-start indices reachable forward from OBB ``head`` up to (not including) the dispatcher."""
    region = set()
    queue = deque([head])
    while queue:
        bb = queue.popleft()
        if bb.start in region:
            continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            succ = edge.target
            if _is_dispatcher_block(mlil, succ):
                continue  # stop at the dispatcher; don't cross into its compare tree
            if succ.start not in region:
                queue.append(succ)
    return region


def _obb_private_exit(mlil, head, region, foreign_ok):
    """The terminating jump of this OBB's last private block before it enters a shared gadget."""
    seen = set()
    queue = deque([head])
    while queue:
        bb = queue.popleft()
        if bb.start in seen:
            continue
        seen.add(bb.start)
        if bb.start != head.start:
            foreign = [
                e.source for e in bb.incoming_edges
                if e.source.start not in region and e.source.start not in foreign_ok
            ]
            if foreign:
                # `bb` is the shared gadget entry. Its in-region predecessor's
                # terminating jump is this OBB's private exit.
                for e in bb.incoming_edges:
                    if e.source.start in region:
                        return mlil[e.source.end - 1]
                return None
        for edge in bb.outgoing_edges:
            if edge.target.start not in seen:
                queue.append(edge.target)
    return None


def _build_shared_store_plan(bv, func, mlil, sm, head, store_il, region, exit_jump):
    """Recover one OBB's transition when its state store is shared with sibling OBBs.

    Resolves ``store_il`` scoped to ``region`` to get this OBB's state constant(s),
    then emits an uncond, cmov_diamond, or cmov_obb plan accordingly.
    Returns a redirection dict, or None if the shape isn't recognised."""
    backbone = sm.backbone
    states = set(resolve_to_constants(func, store_il, scope=region))
    if not states or len(states) > 2:
        log_debug(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"scoped resolve gave {len(states)} state(s); leaving intact"
        )
        return None

    missing = [s for s in states if s not in backbone]
    if missing:
        log_debug(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"state(s) with no backbone entry: {[hex(s) for s in missing]}"
        )
        return None

    if len(states) == 1:
        if exit_jump is None:
            log_warn(
                f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
                f"uncond but no private exit jump found; leaving intact"
            )
            return None
        state_val = states.pop()
        log_info(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)} "
            f"=> uncond {hex(state_val)}"
        )
        return {
            "jump": exit_jump,
            "target_bb": match_successor(bv, backbone[state_val]),
            "obb": head,
            "kind": "uncond",
        }

    # Two states: the OBB fed a cmove whose selection happens in a (possibly
    # shared) diamond -- ``if (cond) <value = alt> else <keep default>``. BN lifts
    # the alternate either as a register copy (``rax_4 = rcx_3``) or, when the
    # source register held a known constant, as a copy-folded constant
    # (``rax_4 = 0xb08bcd8f``) in the then-arm. Rather than pattern-match those
    # forms, find the *deciding branch*: the value-var definition that lives in a
    # direct arm of an MLIL_IF. That arm is the alternate (moved in when the
    # condition fires); the other resolved state is the default.
    value_var = store_il.src.src
    backbone_starts = {bb.start for bb in backbone.values()}
    arms = []  # (const, if_il, arm_is_true_branch)
    for d in mlil.get_var_definitions(value_var):
        blk = d.il_basic_block
        if blk.start not in region or len(blk.incoming_edges) != 1:
            continue
        pred = blk.incoming_edges[0].source
        # The OBB *head* block's sole predecessor is the dispatcher comparator
        # (itself an MLIL_IF), so its default assignment would masquerade as a
        # second cmove arm. A real cmove arm hangs off a program predicate, never
        # off a backbone comparator -- exclude those.
        if pred.start in backbone_starts:
            continue
        cs = set(resolve_to_constants(func, d, scope=region))
        if len(cs) != 1:
            continue
        if_il = mlil[pred.end - 1]
        if if_il.operation.name != "MLIL_IF":
            continue
        arms.append((cs.pop(), if_il, mlil[if_il.true].il_basic_block.start == blk.start))

    if not arms:
        log_debug(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"no cmove arm found among value defs; leaving intact"
        )
        return None
    # A single cmove diamond: every arm must hang off the same MLIL_IF.
    if_il = arms[0][1]
    if any(a[1].expr_index != if_il.expr_index for a in arms):
        log_debug(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"value selected by multiple branches; leaving intact"
        )
        return None
    alt_const, _, then_is_true = arms[0]
    others = [s for s in states if s != alt_const]
    if len(others) != 1:
        return None
    default_const = others[0]
    alt_succ = match_successor(bv, backbone[alt_const])
    default_succ = match_successor(bv, backbone[default_const])

    # Is the diamond *shared* -- fed by sibling OBBs as well as this one? Its block
    # is in this OBB's forward region; a sibling predecessor shows up as an
    # incoming edge whose source lies outside the region.
    diamond_bb = if_il.il_basic_block
    shared = any(e.source.start not in region for e in diamond_bb.incoming_edges)

    if not shared:
        # Private diamond: safe to re-point its arms directly. The arm assigning
        # the alternate goes to the alternate state's OBB; the other to the
        # default state's OBB. We rewrite each arm's *tail* goto (the cmove store
        # and the gadget/dispatcher path are thereby orphaned and dropped as dead).
        plan = {
            "kind": "cmov_diamond",
            "obb": head,
            "if_il": if_il,
            "then_is_true": then_is_true,
            "alt_succ": alt_succ,
            "default_succ": default_succ,
            "default_const": default_const,
            "alt_const": alt_const,
        }
        log_info(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"private cmove diamond @ {hex(if_il.address)} default "
            f"{hex(default_const)} / alt {hex(alt_const)} (then_is_true={then_is_true})"
        )
        return plan

    # Shared diamond: re-pointing its arms would collapse every sibling OBB onto
    # this OBB's two states (N OBBs * 2 states -> 2 states). Instead rewrite *this
    # OBB's own tail*. The OBB computes its condition privately (``cond = <cmp>``)
    # then flows unconditionally into the shared cmove/store gadget. Replace that
    # private exit ``goto`` with a fresh ``if (<copy of cond>) goto alt else goto
    # default``, so each consumer branches on its own predicate to its own states;
    # the shared gadget is never touched and is dropped as dead once every
    # consumer is rewritten.
    if exit_jump is None or exit_jump.operation.name != "MLIL_GOTO":
        log_debug(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"shared diamond but no private exit goto; leaving intact"
        )
        return None
    # The branch needs the OBB's own condition expression. The shared diamond
    # reads it through a variable (``cond:418_1``) that *each* consumer defines in
    # its own tail; resolve that variable to its single in-region definition so we
    # copy this OBB's comparison, not a sibling's or a merge of them.
    cond = if_il.condition
    if cond.operation.name == "MLIL_VAR":
        cdefs = [
            d for d in mlil.get_var_definitions(cond.src)
            if d.il_basic_block.start in region
        ]
        if len(cdefs) != 1:
            log_debug(
                f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
                f"{len(cdefs)} in-region def(s) of the diamond condition; leaving intact"
            )
            return None
        cond_def = cdefs[0]
        cond_src = cond_def.src
    else:
        cond_def = None
        cond_src = cond
    # copy_expr lifts the comparison verbatim, so its operands must still be live
    # where we drop the branch -- only guaranteed when the condition is computed
    # in the very block whose exit goto we replace.
    if cond_def is not None and cond_def.il_basic_block.start != exit_jump.il_basic_block.start:
        log_debug(
            f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: "
            f"diamond condition computed outside the exit block; leaving intact"
        )
        return None

    # CMP_E / CMP_NE alike: the diamond tells us which truth value selects the
    # alternate. If the alt-assigning arm is the true branch, a true condition
    # routes to the alternate state; otherwise a true condition keeps the default.
    if then_is_true:
        true_succ, false_succ = alt_succ, default_succ
    else:
        true_succ, false_succ = default_succ, alt_succ

    log_info(
        f"[deflat] shared-store recover @ {hex(mlil[head.start].address)}: shared "
        f"cmove diamond @ {hex(if_il.address)}; rewrite OBB tail "
        f"{hex(exit_jump.address)} -> true {true_succ.start} / false {false_succ.start} "
        f"(alt {hex(alt_const)}, default {hex(default_const)}, then_is_true={then_is_true})"
    )
    return {
        "kind": "cmov_obb",
        "obb": head,
        "tail_goto": exit_jump,
        "cond_src": cond_src,
        "true_succ": true_succ,
        "false_succ": false_succ,
        "alt_const": alt_const,
        "default_const": default_const,
    }


def recover_shared_store_links(bv, func, sm, gadget_map, handled_exprs):
    """Recover transitions the state machine dropped due to shared state stores, resolved per-OBB."""
    if not sm.shared_stores:
        return []

    mlil = func.medium_level_il
    shared_by_block = {st.il_basic_block.start: st for st in sm.shared_stores}
    backbone_starts = {bb.start for bb in sm.backbone.values()}

    # All OBB heads (a state may share a head; dedupe by start).
    heads = {}
    for comp in sm.backbone.values():
        head = match_successor(bv, comp)
        heads.setdefault(head.start, head)

    plans = []
    for head in heads.values():
        region = _chain_region_blocks(mlil, head)
        store_il = next(
            (shared_by_block[s] for s in region if s in shared_by_block), None
        )
        if store_il is None:
            continue  # this OBB has its own (non-shared) write -> already handled
        # Only the unconditional case needs the OBB's own exit jump; the cmove
        # case re-points the diamond arms in place.
        exit_jump = _obb_private_exit(mlil, head, region, backbone_starts)
        plan = _build_shared_store_plan(bv, func, mlil, sm, head, store_il, region, exit_jump)
        if plan is None:
            continue
        # Dedup on whatever the plan actually rewrites: the private diamond's
        # MLIL_IF (``cmov_diamond``, rewritten once), or the per-OBB exit goto for
        # everything else (``cmov_obb`` shared-diamond tail rewrite and ``uncond``
        # redirects -- each is already per-OBB unique, so this just guards against
        # double-processing).
        if plan["kind"] == "cmov_diamond":
            anchor = plan["if_il"].expr_index
        elif plan["kind"] == "cmov_obb":
            anchor = plan["tail_goto"].expr_index
        else:
            anchor = plan["jump"].expr_index
        if anchor in handled_exprs:
            continue
        handled_exprs.add(anchor)
        plans.append(plan)
    log_info(
        f"[deflat] shared-store recovery: {len(plans)} transition(s) recovered "
        f"from {len(sm.shared_stores)} shared store(s)"
    )
    return plans


def compute_redirections(bv, func, sm=None, gadget_map=None):
    """Read-only: determine which terminating jumps to re-point and where. Returns a list of redirection dicts."""
    mlil = func.medium_level_il
    analysis = _analyze_dispatcher(mlil)
    if analysis is None:
        log_warn(f"[deflat] failed to find dispatcher cluster for function: {hex(func.start)}")
        return []

    redirections = []
    heads = {}
    for head in analysis["token_targets"].values():
        heads.setdefault(head.start, head)
    for head in heads.values():
        plan = _plan_head_transition(mlil, head, analysis)
        if plan is None:
            log_debug(f"[deflat] {head.start}: no v2 transition recovered; leaving intact")
            continue
        redirections.append(plan)
    log_info(
        f"[deflat] v2 recovered {len(redirections)} transition(s) "
        f"from {len(analysis['state_tokens'])} state token(s)"
    )
    return redirections


def _label(operand):
    lbl = MediumLevelILLabel()
    lbl.operand = operand
    return lbl


def _apply_uncond(mlil, r):
    """Replace an unconditional gadget jump with ``goto next_block``."""
    jump = r["jump"]
    target_idx = r["target_bb"].start
    mlil.replace_expr(
        jump.expr_index,
        mlil.goto(_label(target_idx), ILSourceLocation.from_instruction(jump)),
    )
    log_info(
        f"[deflat] {r['obb'].start}: redirect {hex(jump.address)} -> "
        f"{target_idx} ({hex(mlil[target_idx].address)})"
    )
    return 1


def _apply_cmov_diamond(mlil, r):
    """Re-point a cmove diamond's arms to the real successors, orphaning the gadget/dispatcher tail."""
    if_il = r["if_il"]
    then_is_true = r["then_is_true"]
    alt_arm = mlil[if_il.true if then_is_true else if_il.false].il_basic_block
    def_arm = mlil[if_il.false if then_is_true else if_il.true].il_basic_block
    alt_tail = mlil[alt_arm.end - 1]
    def_tail = mlil[def_arm.end - 1]
    mlil.replace_expr(
        alt_tail.expr_index,
        mlil.goto(_label(r["alt_succ"].start), ILSourceLocation.from_instruction(alt_tail)),
    )
    mlil.replace_expr(
        def_tail.expr_index,
        mlil.goto(_label(r["default_succ"].start), ILSourceLocation.from_instruction(def_tail)),
    )
    log_info(
        f"[deflat] {r['obb'].start}: cmove diamond @ {hex(if_il.address)} -> "
        f"alt {r['alt_succ'].start} ({hex(mlil[r['alt_succ'].start].address)}) / "
        f"default {r['default_succ'].start} ({hex(mlil[r['default_succ'].start].address)})"
    )
    return 1


def _apply_cmov_obb(mlil, r):
    """Replace one OBB's exit goto into a shared cmove diamond with a fresh conditional branch."""
    goto_il = r["tail_goto"]
    loc = ILSourceLocation.from_instruction(goto_il)
    mlil.replace_expr(
        goto_il.expr_index,
        mlil.if_expr(
            mlil.copy_expr(r["cond_src"]),
            _label(r["true_succ"].start),
            _label(r["false_succ"].start),
            loc,
        ),
    )
    log_info(
        f"[deflat] {r['obb'].start}: OBB-tail branch @ {hex(goto_il.address)} -> "
        f"true {r['true_succ'].start} ({hex(mlil[r['true_succ'].start].address)}) / "
        f"false {r['false_succ'].start} ({hex(mlil[r['false_succ'].start].address)})"
    )
    return 1


def _apply_if_else(mlil, r):
    """Replace a state-selecting program branch with direct true/false OBB edges."""
    if_il = r["if_il"]
    loc = ILSourceLocation.from_instruction(if_il)
    mlil.replace_expr(
        if_il.expr_index,
        mlil.if_expr(
            mlil.copy_expr(if_il.condition),
            _label(r["true_target"].start),
            _label(r["false_target"].start),
            loc,
        ),
    )
    log_info(
        f"[deflat] {r['obb'].start}: branch @ {hex(if_il.address)} -> "
        f"true {r['true_target'].start} ({hex(mlil[r['true_target'].start].address)}) / "
        f"false {r['false_target'].start} ({hex(mlil[r['false_target'].start].address)})"
    )
    return 1


def _apply_cond_monotone(mlil, r):
    """Forward edge re-pointing for a monotone chain.

    Each cmov diamond's MLIL_IF is kept (its real predicate is preserved via
    ``copy_expr``); only its edges are re-pointed. The branch that takes the
    then-block (assigning the alternate state) goes straight to that state's
    successor; the continuation edge goes to the next diamond, or -- for the
    last diamond, whose continuation is the converged gadget block -- to the
    default state's successor. The then-blocks and the converged gadget block
    are thereby orphaned and dropped as dead by BN; no instruction is removed
    by hand, so no real code is lost."""
    cases_map = r["cases_map"]
    default_succ = cases_map[r["default_const"]].start
    converged_start = r["obb"].start
    for d in r["diamonds"]:
        if_il = d["if_il"]
        then_succ = cases_map[d["then_const"]].start
        cont_bb = d["cont_bb"]
        cont_target = default_succ if cont_bb.start == converged_start else cont_bb.start
        if d["then_is_true"]:
            true_op, false_op = then_succ, cont_target
        else:
            true_op, false_op = cont_target, then_succ
        mlil.replace_expr(
            if_il.expr_index,
            mlil.if_expr(
                mlil.copy_expr(if_il.condition),
                _label(true_op),
                _label(false_op),
                ILSourceLocation.from_instruction(if_il),
            ),
        )
    log_info(
        f"[deflat] {converged_start}: monotone conditional rewritten "
        f"({len(r['diamonds'])} diamonds -> "
        f"{sorted({hex(mlil[b.start].address) for b in cases_map.values()})})"
    )
    
    return 1


def _bool_lit(mlil, cond_il, want_true, loc):
    """A normalised 0/1 boolean for a diamond's MLIL_IF condition (its true branch)
    or its negation, built from a fresh ``copy_expr`` each call so the same source
    condition can be reused across several product terms.

    ``cond != 0`` / ``cond == 0`` rather than the raw expression so AND/OR of several
    of these is true boolean algebra even if a condition isn't already 0/1."""
    c = mlil.copy_expr(cond_il)
    zero = mlil.const(1, 0, loc)
    if want_true:
        return mlil.compare_not_equal(1, c, zero, loc)
    return mlil.compare_equal(1, c, zero, loc)


def _build_cond_predicate(mlil, diamonds, default_const, alt_const, loc):
    """Boolean MLIL expression (sum-of-products) true iff the diamond chain yields ``alt_const``."""
    n = len(diamonds)
    # Enumerate every combination of the diamond conditions (bit i == "MLIL_IF
    # condition i is true"); keep those whose override-fold lands on the alternate.
    terms = []
    for combo in range(1 << n):
        bits = [(combo >> i) & 1 for i in range(n)]
        val = default_const
        for i, d in enumerate(diamonds):
            fires = bool(bits[i]) == bool(d["then_is_true"])
            if fires:
                val = d["then_const"]
        if val == alt_const:
            terms.append(bits)
    if not terms:
        return mlil.const(1, 0, loc)  # never reaches the alternate -> always default

    or_acc = None
    for bits in terms:
        and_acc = None
        for i in range(n):
            lit = _bool_lit(mlil, diamonds[i]["if_il"].condition, bool(bits[i]), loc)
            and_acc = lit if and_acc is None else mlil.and_expr(1, and_acc, lit, loc)
        or_acc = and_acc if or_acc is None else mlil.or_expr(1, or_acc, and_acc, loc)
    return or_acc


def _apply_cond_value(mlil, r):
    """Non-monotone fallback: reconstruct the routing predicate from diamond conditions and branch on it."""
    jump = r["jump"]
    cases_map = r["cases_map"]
    default_const = r["default_const"]
    loc = ILSourceLocation.from_instruction(jump)

    alt_const = next(c for c in cases_map if c != default_const)
    pred = _build_cond_predicate(mlil, r["diamonds"], default_const, alt_const, loc)
    mlil.replace_expr(
        jump.expr_index,
        mlil.if_expr(
            pred,
            _label(cases_map[alt_const].start),
            _label(cases_map[default_const].start),
            loc,
        ),
    )
    log_info(
        f"[deflat] {r['obb'].start}: non-monotone conditional rewritten as "
        f"predicate-branch over {len(r['diamonds'])} diamond condition(s) "
        f"(alt {hex(alt_const)}->{cases_map[alt_const].start} / "
        f"default {hex(default_const)}->{cases_map[default_const].start})"
    )
    return 1


def apply_redirections_il(mlil, redirections, finalize=True):
    """Rewrite each redirection's terminating jump in MLIL. Returns the number of rewrites applied."""
    handlers = {
        "uncond": _apply_uncond,
        "if_else": _apply_if_else,
        "cond": lambda m, r: (_apply_cond_monotone if r["monotone"] else _apply_cond_value)(m, r),
        "cmov_diamond": _apply_cmov_diamond,
        "cmov_obb": _apply_cmov_obb,
    }
    applied = 0
    for r in redirections:
        handler = handlers.get(r["kind"])
        if handler is None:
            continue
        try:
            applied += handler(mlil, r)
        except Exception as e:  # noqa: BLE001
            anchor = r.get("jump") or r.get("if_il") or r.get("tail_goto")
            where = anchor.address if anchor is not None else 0
            log_warn(f"[deflat] failed to rewrite {hex(where)}: {e}")
    if applied and finalize:
        mlil.finalize()
        mlil.generate_ssa_form()
    return applied
