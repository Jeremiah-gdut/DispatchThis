"""Deflattening core: turn ``OBB -> dispatcher`` edges into ``OBB -> next OBB``.

``compute_redirections`` -- read-only analysis recovering dispatcher state-token
transitions to determine which jumps/branches to re-point.
``apply_redirections_il`` -- rewrites MLIL in place; only meaningful inside a workflow activity.
"""

from binaryninja import (
    ILSourceLocation,
    MediumLevelILLabel,
)

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


def _plan_entry_transition(mlil, analysis):
    stop_starts = analysis["dispatcher_starts"]
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    token_targets = analysis["token_targets"]
    if not mlil.basic_blocks:
        return None

    region = set()
    exits = []
    queue = deque([mlil.basic_blocks[0]])
    while queue:
        bb = queue.popleft()
        if bb.start in region or bb.start in stop_starts:
            continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            if edge.target.start in stop_starts:
                exits.append(_last(mlil, bb))
            elif edge.target.start not in region:
                queue.append(edge.target)

    if len(exits) != 1:
        return None
    tokens = _state_write_tokens(mlil, root, token_size, region)
    if len(tokens) != 1:
        return None
    token = next(iter(tokens))
    target = token_targets.get(token)
    if target is None:
        return None
    return {
        "jump": exits[0],
        "target_bb": target,
        "obb": mlil.basic_blocks[0],
        "kind": "uncond",
        "state_var": root,
        "state_vars": _selection_vars(mlil, root, region),
        "state_token": token,
        "state_tokens": {token},
        "entry": True,
    }


def compute_redirections(bv, func, gadget_map=None, mlil=None):
    """Read-only: determine which terminating jumps to re-point and where. Returns a list of redirection dicts."""
    mlil = mlil or func.medium_level_il
    analysis = _analyze_dispatcher(mlil)
    if analysis is None:
        log_warn(f"[deflat] failed to find dispatcher cluster for function: {hex(func.start)}")
        return []

    redirections = []
    entry_plan = _plan_entry_transition(mlil, analysis)
    if entry_plan is not None:
        redirections.append(entry_plan)
    else:
        log_debug("[deflat] no entry-state transition recovered; entry dispatcher path left intact")

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


def apply_redirections_il(mlil, redirections, finalize=True):
    """Rewrite each redirection's terminating jump in MLIL. Returns the number of rewrites applied."""
    handlers = {
        "uncond": _apply_uncond,
        "if_else": _apply_if_else,
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
