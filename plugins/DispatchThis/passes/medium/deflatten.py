"""Deflattening core: turn ``OBB -> dispatcher`` edges into ``OBB -> next OBB``.

``compute_redirections`` -- read-only analysis recovering dispatcher state-token
transitions to determine which jumps/branches to re-point.
``rewrite_redirections_mlil`` -- creates an atomic replacement MLIL function.
"""

from collections import deque

from binaryninja import (
    ILSourceLocation,
)

from ...helpers import mlil as mlil_helpers
from ...utils.log import log_info, log_warn, log_debug
from .rewrite import copied_label_for_source, copy_mlil_with_instruction_rewrites


def _last(mlil, bb):
    return mlil[bb.end - 1]


def _block_at(mlil, instr_index):
    return mlil[instr_index].il_basic_block


_op = mlil_helpers.op_name
_same_var = mlil_helpers.same_var
_state_token = mlil_helpers.state_token
_direct_var_from_expr = mlil_helpers.direct_var_from_expr


_evaluate_comparison = mlil_helpers.evaluate_comparison


def _comparison_var(cond):
    if not (_op(cond) or "").startswith("MLIL_CMP"):
        return None
    variables = [
        var
        for var in (
            _direct_var_from_expr(getattr(cond, "left", None)),
            _direct_var_from_expr(getattr(cond, "right", None)),
        )
        if var is not None
    ]
    return variables[0] if len(variables) == 1 else None


def _comparison_details(mlil, if_il):
    condition = getattr(if_il, "condition", None)
    definition = None
    if _op(condition) == "MLIL_VAR":
        try:
            ssa_var = condition.ssa_form.src
            ssa_definition = condition.function.ssa_form.get_ssa_var_definition(
                ssa_var
            )
            definition = mlil_helpers.current_non_ssa_instruction(
                mlil,
                ssa_definition,
            )
        except (AttributeError, KeyError, IndexError, TypeError):
            return None
        definition_block = getattr(definition, "il_basic_block", None)
        if (
            definition is None
            or _op(definition) != "MLIL_SET_VAR"
            or not _same_var(getattr(definition, "dest", None), condition.src)
            or definition_block is None
            or definition_block.start != if_il.il_basic_block.start
            or definition.instr_index >= if_il.instr_index
        ):
            return None
        condition = getattr(definition, "src", None)
    return {
        "condition": condition,
        "definition": definition,
        "parts": mlil_helpers.comparison_parts(condition),
        "use": definition or if_il,
        "var": _comparison_var(condition),
    }


def _router_prefix_is_pure(bb, details, state_var, state_vars):
    definition = details["definition"]
    definition_index = (
        getattr(definition, "instr_index", None)
        if definition is not None
        else None
    )
    saw_definition = definition is None
    for ins in list(bb)[:-1]:
        if definition_index is not None and ins.instr_index == definition_index:
            saw_definition = True
            continue
        if not _pure_router_instruction(ins, state_var, state_vars):
            return False
    return saw_definition


def _dispatcher_rows(mlil):
    rows = []
    for bb in mlil.basic_blocks:
        last = _last(mlil, bb)
        if _op(last) != "MLIL_IF":
            continue
        details = _comparison_details(mlil, last)
        if details is None or details["parts"] is None:
            continue
        parts = details["parts"]
        chain = mlil_helpers.row_local_copy_chain(
            mlil,
            parts["var"],
            bb,
            details["use"],
        )
        if chain is None:
            continue
        state_vars = set(chain)
        if details["definition"] is not None:
            state_vars.add(details["definition"].dest)
        rows.append(
            {
                "bb": bb,
                "if_il": last,
                "var": parts["var"],
                "root": chain[-1],
                "state_vars": state_vars,
                "comparison": parts,
                "comparison_details": details,
            }
        )
    return rows


def _pure_router_instruction(ins, state_var, state_vars):
    op = _op(ins)
    if op in {"MLIL_GOTO", "MLIL_NOP"}:
        return True
    if op != "MLIL_SET_VAR" or _same_var(getattr(ins, "dest", None), state_var):
        return False
    source = getattr(ins, "src", None)
    source_var = _direct_var_from_expr(source)
    dest_size = getattr(ins, "size", None)
    source_size = getattr(source, "size", None)
    if dest_size is not None and source_size is not None and dest_size != source_size:
        return False
    return source_var is not None and all(
        any(_same_var(var, candidate) for candidate in state_vars)
        for var in (ins.dest, source_var)
    )


def _router_boundary_block(mlil, bb, state_var, state_vars):
    last = _last(mlil, bb)
    if _op(last) == "MLIL_IF":
        details = _comparison_details(mlil, last)
        if (
            details is None
            or details["parts"] is None
            or not _router_prefix_is_pure(
                bb,
                details,
                state_var,
                state_vars,
            )
        ):
            return False
        parts = details["parts"]
        chain = mlil_helpers.row_local_copy_chain(
            mlil,
            parts["var"],
            bb,
            details["use"],
        )
        return chain is not None and _same_var(chain[-1], state_var)
    return all(
        _pure_router_instruction(ins, state_var, state_vars)
        for ins in bb
    )


def _expand_dispatcher_boundary(mlil, starts, state_var, state_vars):
    expanded = set(starts)
    by_start = {bb.start: bb for bb in mlil.basic_blocks}
    queue = deque(by_start[start] for start in starts if start in by_start)
    while queue:
        bb = queue.popleft()
        for edge in bb.incoming_edges:
            pred = edge.source
            if pred.start in expanded or not _router_boundary_block(
                mlil,
                pred,
                state_var,
                state_vars,
            ):
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
        key = (row["root"], row["comparison"]["bound"][1])
        groups.setdefault(key, []).append(row)
    candidate_groups = [group for group in groups.values() if len(group) >= 3]
    if not candidate_groups:
        return None
    candidate_groups.sort(key=len, reverse=True)
    if len(candidate_groups) > 1 and len(candidate_groups[1]) * 2 >= len(candidate_groups[0]):
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
    sizes = {row["comparison"]["bound"][1] for row in rows}
    state_var = next(iter(roots))
    state_vars = set().union(*(row["state_vars"] for row in rows))
    if any(
        not _router_boundary_block(mlil, row["bb"], state_var, state_vars)
        for row in rows
    ):
        log_warn("[deflat] dispatcher row contains an impure state update; skipping")
        return None

    dispatcher_starts = {row["bb"].start for row in rows}
    for bb in mlil.basic_blocks:
        last = _last(mlil, bb)
        if _op(last) != "MLIL_IF":
            continue
        details = _comparison_details(mlil, last)
        if details is None:
            continue
        parts = details["parts"]
        comparison_var = parts["var"] if parts is not None else details["var"]
        if comparison_var is None:
            continue
        chain = mlil_helpers.row_local_copy_chain(
            mlil,
            comparison_var,
            bb,
            details["use"],
        )
        if chain is None or not _same_var(chain[-1], state_var):
            continue
        row_state_vars = set(chain)
        if details["definition"] is not None:
            row_state_vars.add(details["definition"].dest)
        candidate_state_vars = state_vars | row_state_vars
        if not _router_boundary_block(
            mlil,
            bb,
            state_var,
            candidate_state_vars,
        ):
            continue
        dispatcher_starts.add(bb.start)
        state_vars.update(row_state_vars)
    dispatcher_starts = _expand_dispatcher_boundary(
        mlil,
        dispatcher_starts,
        state_var,
        state_vars,
    )

    target_heads = {}
    for bb in mlil.basic_blocks:
        if bb.start not in dispatcher_starts:
            continue
        for edge in bb.outgoing_edges:
            if edge.target.start not in dispatcher_starts:
                target_heads.setdefault(edge.target.start, edge.target)

    return {
        "state_var": state_var,
        "state_vars": state_vars,
        "state_address_escapes": mlil_helpers.variable_address_escapes(
            mlil,
            state_var,
        ),
        "token_size": next(iter(sizes)),
        "dispatcher_starts": dispatcher_starts,
        "dispatcher_rows": {row["bb"].start: row for row in rows},
        "target_heads": tuple(target_heads.values()),
    }


def _region_until(mlil, start_bb, stop_starts, state_var=None):
    region = set()
    queue = deque([start_bb])
    while queue:
        bb = queue.popleft()
        if bb.start in region or bb.start in stop_starts:
            continue
        region.add(bb.start)
        for edge in bb.outgoing_edges:
            if edge.target.start not in region and edge.target.start not in stop_starts:
                queue.append(edge.target)
    return region


def _private_exits(mlil, head, region, stop_starts):
    exits = {}
    for bb in mlil.basic_blocks:
        if bb.start not in region:
            continue
        foreign = [
            edge.source
            for edge in bb.incoming_edges
            if edge.source.start not in region
        ]
        if bb.start != head.start and foreign:
            return ()
        for edge in bb.outgoing_edges:
            if edge.target.start not in stop_starts:
                continue
            jump = _last(mlil, bb)
            if _op(jump) != "MLIL_GOTO" or len(bb.outgoing_edges) != 1:
                return ()
            exits[jump.instr_index] = (jump, edge.target)
    return tuple(exits.values())


def _route_dispatcher_token(mlil, analysis, start, token):
    dispatcher_starts = analysis["dispatcher_starts"]
    rows = analysis["dispatcher_rows"]
    current = start
    seen = set()
    while current.start in dispatcher_starts:
        if current.start in seen:
            return None
        seen.add(current.start)
        row = rows.get(current.start)
        if row is not None:
            branch = _evaluate_comparison(row["comparison"], token)
            if branch is None:
                return None
            current = _block_at(mlil, row["if_il"].true if branch else row["if_il"].false)
            continue
        if _op(_last(mlil, current)) == "MLIL_IF":
            return None
        outgoing = list(current.outgoing_edges)
        if len(outgoing) != 1:
            return None
        current = outgoing[0].target
    return current


def _route_scope(mlil, analysis, scope, token):
    exits = {}
    for bb in mlil.basic_blocks:
        if bb.start not in scope:
            continue
        for edge in bb.outgoing_edges:
            if edge.target.start in analysis["dispatcher_starts"]:
                jump = list(bb)[-1]
                if _op(jump) != "MLIL_GOTO" or len(bb.outgoing_edges) != 1:
                    return None
                exits[jump.instr_index] = (jump, edge.target)
            elif edge.target.start not in scope:
                return None
    if not exits or not mlil_helpers.all_paths_reach_stops(
        mlil.basic_blocks,
        scope,
        analysis["dispatcher_starts"],
    ):
        return None
    targets = {}
    for _jump, start in exits.values():
        target = _route_dispatcher_token(mlil, analysis, start, token)
        if target is None:
            return None
        targets.setdefault(target.start, target)
    if len(targets) != 1:
        return None
    return {
        "target": next(iter(targets.values())),
        "exits": tuple(exits.values()),
        "direct_rows": all(
            entry.start in analysis["dispatcher_rows"]
            for _jump, entry in exits.values()
        ),
    }


def _resolve_tokens_from_expr(mlil, expr, token_size, scope, seen=None):
    if seen is None:
        seen = set()
    op = _op(expr)
    if op == "MLIL_CONST":
        return {_state_token(expr, token_size)}
    source_var = _direct_var_from_expr(expr)
    if source_var is None:
        return None
    if source_var in seen:
        return None
    seen = set(seen)
    seen.add(source_var)
    tokens = set()
    found = False
    try:
        definitions = list(mlil.get_var_definitions(source_var))
    except Exception:  # noqa: BLE001
        return None
    for definition in definitions:
        definition_block = getattr(definition, "il_basic_block", None)
        if definition_block is None or definition_block.start not in scope:
            continue
        found = True
        resolved = _resolve_tokens_from_expr(
            mlil,
            definition.src,
            token_size,
            scope,
            set(seen),
        )
        if not resolved:
            return None
        tokens.update(resolved)
    return tokens if found else None


def _state_writes(mlil, root, token_size, scope, state_address_escapes):
    writes = []
    for bb in mlil.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            operation = _op(ins)
            if mlil_helpers.has_unmodeled_semantics(ins):
                return None
            if operation == "MLIL_SET_VAR" and _same_var(ins.dest, root):
                resolved = _resolve_tokens_from_expr(mlil, ins.src, token_size, scope)
                if resolved is None or len(resolved) != 1:
                    return None
                writes.append((ins, next(iter(resolved))))
                continue
            if mlil_helpers.instruction_writes_variable(ins, root):
                return None
            if (
                operation in mlil_helpers.STORE_OPS
                and mlil_helpers.expression_may_address_variable(
                    mlil,
                    getattr(ins, "dest", None),
                    root,
                )
            ):
                return None
            if (
                operation in mlil_helpers.STORE_OPS
                and state_address_escapes
            ):
                return None
            if (
                mlil_helpers.has_unknown_memory_effect(ins)
                and (
                    state_address_escapes
                    or mlil_helpers.expression_may_address_variable(
                        mlil,
                        ins,
                        root,
                    )
                )
            ):
                return None
    return writes


def _single_state_transition(
    mlil,
    root,
    token_size,
    scope,
    start,
    state_address_escapes,
):
    writes = _state_writes(
        mlil,
        root,
        token_size,
        scope,
        state_address_escapes,
    )
    if not writes:
        return None
    tokens = {token for _ins, token in writes}
    if len(tokens) != 1:
        return None
    if not mlil_helpers.all_paths_hit_blocks(
        mlil.basic_blocks,
        {start.start},
        scope,
        {ins.il_basic_block.start for ins, _token in writes},
    ):
        return None
    if not mlil_helpers.definitions_cover_all_paths(
        mlil,
        {start.start},
        scope,
        (getattr(ins, "src", None) for ins, _token in writes),
    ):
        return None
    return next(iter(tokens)), writes


def _region_is_private(mlil, scope, owners):
    allowed = set(scope) | set(owners)
    return not any(
        bb.start not in owners
        and any(edge.source.start not in allowed for edge in bb.incoming_edges)
        for bb in mlil.basic_blocks
        if bb.start in scope
    )


def _owned_write_indices(mlil, writes, scope, owners):
    if not _region_is_private(mlil, scope, owners):
        return set()
    return {ins.instr_index for ins, _token in writes}


def _state_vars_are_dispatcher_only(mlil, analysis, state_vars, ignored_scope=()):
    dispatcher_starts = analysis["dispatcher_starts"]
    ignored_scope = set(ignored_scope)
    for bb in mlil.basic_blocks:
        if bb.start in dispatcher_starts or bb.start in ignored_scope:
            continue
        for ins in bb:
            if _op(ins) != "MLIL_SET_VAR" and any(
                mlil_helpers.instruction_writes_variable(ins, state_var)
                for state_var in state_vars
            ):
                return False
            if any(
                mlil_helpers.instruction_reads_variable(ins, state_var)
                for state_var in state_vars
            ):
                return False
            for expr in mlil_helpers.walk_expr(ins):
                addressed = mlil_helpers.addressed_var(expr)
                if addressed is not None and any(
                    _same_var(addressed, state_var)
                    for state_var in state_vars
                ):
                    return False
    return True


def _state_channel_is_dispatcher_only(mlil, analysis, ignored_scope=()):
    return _state_vars_are_dispatcher_only(
        mlil,
        analysis,
        analysis["state_vars"],
        ignored_scope,
    )


def _dispatcher_values_are_private(mlil, analysis):
    root = analysis["state_var"]
    dispatcher_values = {
        state_var
        for state_var in analysis["state_vars"]
        if not _same_var(state_var, root)
    }
    return _state_vars_are_dispatcher_only(
        mlil,
        analysis,
        dispatcher_values,
    )


def _pure_state_selection_tail(mlil, root, scope, writes):
    required = mlil_helpers.dependency_variables(
        mlil,
        (getattr(ins, "src", None) for ins, _token in writes),
        scope,
    )
    written_dependencies = set()
    for bb in mlil.basic_blocks:
        if bb.start not in scope:
            continue
        for ins in bb:
            op = _op(ins)
            if op in {"MLIL_IF", "MLIL_GOTO", "MLIL_NOP"}:
                continue
            if op != "MLIL_SET_VAR":
                return False
            if _same_var(ins.dest, root):
                continue
            if not any(_same_var(ins.dest, var) for var in required):
                return False
            written_dependencies.add(ins.dest)
    return mlil_helpers.variables_are_scope_local(mlil, written_dependencies, scope)


def _conditional_candidates(mlil, head, region, analysis):
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    stop_starts = analysis["dispatcher_starts"]
    candidates = []
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
        true_scope = _region_until(mlil, true_bb, stop_starts, root)
        false_scope = _region_until(mlil, false_bb, stop_starts, root)
        true_transition = _single_state_transition(
            mlil,
            root,
            token_size,
            true_scope,
            true_bb,
            analysis["state_address_escapes"],
        )
        false_transition = _single_state_transition(
            mlil,
            root,
            token_size,
            false_scope,
            false_bb,
            analysis["state_address_escapes"],
        )
        if true_transition is None or false_transition is None:
            continue
        true_token, true_writes = true_transition
        false_token, false_writes = false_transition
        if true_token == false_token:
            continue
        writes = (*true_writes, *false_writes)
        scopes = true_scope | false_scope
        if not _pure_state_selection_tail(mlil, root, scopes, writes):
            continue
        true_route = _route_scope(mlil, analysis, true_scope, true_token)
        false_route = _route_scope(mlil, analysis, false_scope, false_token)
        if true_route is None or false_route is None:
            continue
        owned_writes = _owned_write_indices(mlil, writes, scopes, {head.start})
        exit_cleanup = (
            owned_writes
            if _state_channel_is_dispatcher_only(mlil, analysis)
            else set()
        )
        exit_targets = {}
        conflicting_exit = False
        for route in (true_route, false_route):
            for jump, _entry in route["exits"]:
                prior = exit_targets.get(jump.instr_index)
                if prior is not None and prior[1].start != route["target"].start:
                    conflicting_exit = True
                    break
                exit_targets[jump.instr_index] = (jump, route["target"])
            if conflicting_exit:
                break
        exact_writes = {ins.instr_index for ins, _token in writes}
        preserve_writes = (
            not conflicting_exit
            and true_route["direct_rows"]
            and false_route["direct_rows"]
            and owned_writes == exact_writes
            and _dispatcher_values_are_private(mlil, analysis)
        )
        bypass_safe = (
            owned_writes == exact_writes
            and _state_channel_is_dispatcher_only(mlil, analysis, scopes)
        )
        if not preserve_writes and not bypass_safe:
            continue
        candidates.append(
            {
                "kind": "if_else",
                "rewrite_mode": "arm_exits" if preserve_writes else "condition",
                "obb": head,
                "if_il": if_il,
                "true_target": true_route["target"],
                "false_target": false_route["target"],
                "true_token": true_token,
                "false_token": false_token,
                "exit_targets": (
                    tuple(exit_targets.values()) if preserve_writes else ()
                ),
                "obsolete_state_writes": exit_cleanup if preserve_writes else owned_writes,
            }
        )
    return tuple(candidates)


def _plan_head_transition(mlil, head, analysis):
    stop_starts = analysis["dispatcher_starts"]
    root = analysis["state_var"]
    token_size = analysis["token_size"]
    region = _region_until(mlil, head, stop_starts, root)

    conditional = _conditional_candidates(mlil, head, region, analysis)
    if len(conditional) > 1:
        log_warn(f"[deflat] {head.start}: ambiguous conditional transitions; skipping")
        return None
    if conditional:
        return conditional[0]

    transition = _single_state_transition(
        mlil,
        root,
        token_size,
        region,
        head,
        analysis["state_address_escapes"],
    )
    if transition is None:
        return None
    if not mlil_helpers.all_paths_reach_stops(
        mlil.basic_blocks,
        region,
        stop_starts,
    ):
        return None
    token, writes = transition
    exits = _private_exits(mlil, head, region, stop_starts)
    if not exits:
        return None
    targets = {}
    for _jump, dispatcher_entry in exits:
        target = _route_dispatcher_token(mlil, analysis, dispatcher_entry, token)
        if target is None:
            return None
        targets.setdefault(target.start, target)
    if len(targets) != 1:
        return None
    if not _dispatcher_values_are_private(mlil, analysis):
        return None
    return {
        "exit_jumps": tuple(jump for jump, _entry in exits),
        "target_bb": next(iter(targets.values())),
        "obb": head,
        "kind": "uncond",
        "state_token": token,
        "obsolete_state_writes": (
            _owned_write_indices(mlil, writes, region, {head.start})
            if _state_channel_is_dispatcher_only(mlil, analysis)
            else set()
        ),
    }


def _plan_entry_transition(mlil, analysis):
    stop_starts = analysis["dispatcher_starts"]
    root = analysis["state_var"]
    token_size = analysis["token_size"]
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
                exits.append((_last(mlil, bb), edge.target))
            elif edge.target.start not in region:
                queue.append(edge.target)

    if not exits or any(
        _op(jump) != "MLIL_GOTO" or len(jump.il_basic_block.outgoing_edges) != 1
        for jump, _entry in exits
    ):
        return None
    entry_start = mlil.basic_blocks[0].start
    if any(
        jump.il_basic_block.start != entry_start
        and any(
            edge.source.start not in region
            for edge in jump.il_basic_block.incoming_edges
        )
        for jump, _entry in exits
    ):
        return None
    transition = _single_state_transition(
        mlil,
        root,
        token_size,
        region,
        mlil.basic_blocks[0],
        analysis["state_address_escapes"],
    )
    if transition is None:
        return None
    if not _region_is_private(mlil, region, {mlil.basic_blocks[0].start}):
        return None
    if not mlil_helpers.all_paths_reach_stops(
        mlil.basic_blocks,
        region,
        stop_starts,
    ):
        return None
    token, writes = transition
    targets = {}
    for _jump, dispatcher_entry in exits:
        target = _route_dispatcher_token(mlil, analysis, dispatcher_entry, token)
        if target is None:
            return None
        targets.setdefault(target.start, target)
    if len(targets) != 1:
        return None
    if not _dispatcher_values_are_private(mlil, analysis):
        return None
    return {
        "exit_jumps": tuple(jump for jump, _entry in exits),
        "target_bb": next(iter(targets.values())),
        "obb": mlil.basic_blocks[0],
        "kind": "uncond",
        "state_token": token,
        "obsolete_state_writes": (
            _owned_write_indices(
                mlil,
                writes,
                region,
                {mlil.basic_blocks[0].start},
            )
            if _state_channel_is_dispatcher_only(mlil, analysis)
            else set()
        ),
        "entry": True,
    }


def compute_redirections(bv, func, mlil=None):
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
    for head in analysis["target_heads"]:
        heads.setdefault(head.start, head)
    for head in heads.values():
        plan = _plan_head_transition(mlil, head, analysis)
        if plan is None:
            log_debug(f"[deflat] {head.start}: no v2 transition recovered; leaving intact")
            continue
        redirections.append(plan)
    log_info(f"[deflat] v2 recovered {len(redirections)} transition(s)")
    return redirections


def _copy_condition(mlil, condition):
    copy_to = getattr(condition, "copy_to", None)
    return copy_to(mlil) if copy_to is not None else mlil.copy_expr(condition)


def _valid_instruction_index(instr_index):
    return type(instr_index) is int and instr_index >= 0


def _replacements_for_redirection(redirection):
    kind = redirection.get("kind")
    if kind == "uncond":
        target_start = getattr(redirection.get("target_bb"), "start", None)
        if not _valid_instruction_index(target_start):
            return None

        def replace(mlil, jump):
            return mlil.goto(
                copied_label_for_source(mlil, target_start),
                ILSourceLocation.from_instruction(jump),
            )

        raw_jumps = redirection.get("exit_jumps")
        if not isinstance(raw_jumps, (tuple, list)):
            return None
        jumps = tuple(raw_jumps)
        if not jumps or any(_op(jump) != "MLIL_GOTO" for jump in jumps):
            return None
        return tuple((jump, replace) for jump in jumps)

    if kind == "if_else":
        rewrite_mode = redirection.get("rewrite_mode")
        if rewrite_mode == "arm_exits":
            raw_exit_targets = redirection.get("exit_targets")
            if not isinstance(raw_exit_targets, (tuple, list)):
                return None
            exit_targets = tuple(raw_exit_targets)
            if not exit_targets:
                return None
            replacements = []
            seen = {}
            for item in exit_targets:
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    return None
                jump, target = item
                if _op(jump) != "MLIL_GOTO":
                    return None
                target_start = getattr(target, "start", None)
                if not _valid_instruction_index(target_start):
                    return None
                prior = seen.get(jump.instr_index)
                if prior is not None:
                    if prior != target_start:
                        return None
                    continue
                seen[jump.instr_index] = target_start

                def replace(mlil, old_jump, copied_target=target_start):
                    return mlil.goto(
                        copied_label_for_source(mlil, copied_target),
                        ILSourceLocation.from_instruction(old_jump),
                    )

                replacements.append((jump, replace))
            return tuple(replacements)

        if rewrite_mode != "condition":
            return None

        true_start = getattr(redirection.get("true_target"), "start", None)
        false_start = getattr(redirection.get("false_target"), "start", None)
        if not all(
            _valid_instruction_index(start)
            for start in (true_start, false_start)
        ):
            return None

        def replace(mlil, if_il):
            return mlil.if_expr(
                _copy_condition(mlil, if_il.condition),
                copied_label_for_source(mlil, true_start),
                copied_label_for_source(mlil, false_start),
                ILSourceLocation.from_instruction(if_il),
            )

        if_il = redirection.get("if_il")
        if _op(if_il) != "MLIL_IF":
            return None
        return ((if_il, replace),)

    return None


def _nop_state_write(mlil, state_write):
    return mlil.nop(ILSourceLocation.from_instruction(state_write))


def _current_plan_source(mlil, source):
    instr_index = getattr(source, "instr_index", None)
    source_expr = getattr(source, "expr_index", None)
    if not _valid_instruction_index(instr_index) or source_expr is None:
        return None
    try:
        current = mlil[instr_index]
    except Exception:  # noqa: BLE001
        return None
    if (
        _op(current) != _op(source)
        or getattr(current, "instr_index", None) != instr_index
        or getattr(current, "expr_index", None) != source_expr
        or getattr(current, "address", None) != getattr(source, "address", None)
    ):
        return None
    return current


def rewrite_redirections_mlil(ctx, mlil, redirections):
    """Create an atomic replacement MLIL function for planned redirections."""
    if mlil is None:
        return mlil, 0
    if not isinstance(redirections, (tuple, list)):
        return None, 0
    if not redirections:
        return mlil, 0

    replacements = {}
    cleanup_indices = set()
    for redirection in redirections:
        if not isinstance(redirection, dict):
            return None, 0
        planned = _replacements_for_redirection(redirection)
        if planned is None:
            return None, 0
        for source, replacement in planned:
            current = _current_plan_source(mlil, source)
            if current is None or current.instr_index in replacements:
                return None, 0
            replacements[current.instr_index] = replacement
        cleanup = redirection.get("obsolete_state_writes")
        if not isinstance(cleanup, set) or any(
            not _valid_instruction_index(instr_index)
            for instr_index in cleanup
        ):
            return None, 0
        cleanup_indices.update(cleanup)

    if cleanup_indices & set(replacements):
        return None, 0
    for instr_index in cleanup_indices:
        try:
            state_write = mlil[instr_index]
        except Exception:  # noqa: BLE001
            return None, 0
        if (
            getattr(state_write, "instr_index", None) != instr_index
            or _op(state_write) not in ("MLIL_SET_VAR", "MLIL_STORE")
        ):
            return None, 0
        replacements[instr_index] = _nop_state_write

    new_mlil, applied = copy_mlil_with_instruction_rewrites(ctx, replacements, mlil=mlil)
    return (new_mlil, len(redirections)) if applied == len(replacements) else (None, 0)
