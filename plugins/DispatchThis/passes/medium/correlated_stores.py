"""Apply validated path-correlated global-store recovery plans."""

from binaryninja import ILSourceLocation, MediumLevelILOperation as M

from ...helpers._values_identity import same_entity
from ...helpers.mlil import (
    LOAD_OPERATIONS,
    current_non_ssa_instruction,
    expression_has_operation,
    has_unknown_memory_effect,
    has_unmodeled_semantics,
    operation,
)
from ...semantics import CorrelatedStoreArm, CorrelatedStorePlan

from .rewrite import copy_mlil_with_instruction_rewrites


_PURE_JOIN_OPERATIONS = (M.MLIL_SET_VAR, M.MLIL_SET_VAR_FIELD, M.MLIL_NOP)
_CONCRETE_ADDRESS_OPERATIONS = (M.MLIL_CONST, M.MLIL_CONST_PTR)
_SKIP = object()


def _valid_index(value):
    return type(value) is int and value >= 0


def _expression_witness(expr):
    expr_index = getattr(expr, "expr_index", None)
    op = operation(expr)
    if not _valid_index(expr_index) or op is None:
        return None
    return expr_index, op, getattr(expr, "size", None)


def _operand_witness(instruction, expected_operation):
    if expected_operation == M.MLIL_GOTO:
        dest = getattr(instruction, "dest", None)
        return (dest,) if _valid_index(dest) else None
    if expected_operation == M.MLIL_STORE:
        dest = _expression_witness(getattr(instruction, "dest", None))
        src = _expression_witness(getattr(instruction, "src", None))
        return None if dest is None or src is None else (dest, src)
    return None


def _current_instruction(mlil, instruction, expected_operation):
    expected = getattr(instruction, "non_ssa_form", None)
    if expected is None:
        expected = instruction
    values = tuple(getattr(expected, name, None) for name in ("instr_index", "expr_index", "address"))
    if any(not _valid_index(value) for value in values):
        return None
    current = current_non_ssa_instruction(mlil, expected)
    if (
        current is None
        or getattr(expected, "function", None) is not mlil
        or getattr(current, "function", None) is not mlil
        or operation(current) != expected_operation
    ):
        return None
    if any(
        type(getattr(current, name, None)) is not int
        or getattr(current, name) != value
        for name, value in zip(("instr_index", "expr_index", "address"), values)
    ):
        return None
    witness = _operand_witness(expected, expected_operation)
    if witness is None or witness != _operand_witness(current, expected_operation):
        return None
    return current


def _current_expression(mlil, expression):
    expected = getattr(expression, "non_ssa_form", None)
    if expected is None:
        expected = expression
    values = tuple(getattr(expected, name, None) for name in ("instr_index", "expr_index", "address"))
    if any(not _valid_index(value) for value in values):
        return None
    getter = getattr(mlil, "get_expr", None)
    if getter is None or getattr(expected, "function", None) is not mlil:
        return None
    current = getter(values[1])
    if (
        current is None
        or getattr(current, "function", None) is not mlil
        or operation(current) != operation(expected)
        or getattr(current, "size", None) != getattr(expected, "size", None)
        or getattr(current, "constant", None) != getattr(expected, "constant", None)
    ):
        return None
    if any(
        type(getattr(current, name, None)) is not int
        or getattr(current, name) != value
        for name, value in zip(("instr_index", "expr_index", "address"), values)
    ):
        return None
    return current


def _edges(block, name):
    return tuple(getattr(block, name, ()) or ())


def _same_entities(left, right):
    return len(left) == len(right) and all(sum(same_entity(candidate, expected) for candidate in right) == 1 for expected in left)


def _block_range(block):
    start = getattr(block, "start", None)
    end = getattr(block, "end", None)
    return (start, end) if _valid_index(start) and type(end) is int and end > start else None


def _concrete_address(expression, address):
    return (
        operation(expression) in _CONCRETE_ADDRESS_OPERATIONS
        and type(getattr(expression, "constant", None)) is int
        and getattr(expression, "constant") == address
    )


def _pure_join_prefix(mlil, join, store):
    block_range = _block_range(join)
    store_index = getattr(store, "instr_index", None)
    if block_range is None or not _valid_index(store_index):
        return False
    start, end = block_range
    if not start <= store_index < end:
        return False
    for index in range(start, store_index):
        try:
            instruction = mlil[index]
            impure = (
                operation(instruction) not in _PURE_JOIN_OPERATIONS
                or has_unknown_memory_effect(instruction)
                or has_unmodeled_semantics(instruction)
                or expression_has_operation(instruction, LOAD_OPERATIONS)
            )
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — current MLIL proof must fail closed.
            return False
        if getattr(instruction, "instr_index", None) != index or impure:
            return False
    return True


def _typed_plan(plan):
    arms = getattr(plan, "arms", None)
    return (
        type(plan) is CorrelatedStorePlan
        and type(getattr(plan, "size", None)) is int
        and getattr(plan, "size") > 0
        and type(arms) is tuple
        and len(arms) == 2
        and all(type(arm) is CorrelatedStoreArm for arm in arms)
    )


def _validated_plan(mlil, plan):
    store = _current_instruction(mlil, plan.store_il, M.MLIL_STORE)
    store_index = getattr(store, "instr_index", None)
    join = getattr(store, "il_basic_block", None)
    join_range = _block_range(join)
    if (
        not same_entity(plan.join_block, join)
        or join_range is None
        or getattr(store, "size", None) != plan.size
    ):
        return None
    join_edges = _edges(join, "incoming_edges")
    if len(join_edges) != 2:
        return None

    arms = []
    arm_edges = []
    arm_entries = []
    head = None
    for arm in plan.arms:
        goto = _current_instruction(mlil, arm.goto_il, M.MLIL_GOTO)
        goto_index = getattr(goto, "instr_index", None)
        predecessor = arm.predecessor
        edge = arm.incoming_edge
        predecessor_range = _block_range(predecessor)
        outgoing = _edges(predecessor, "outgoing_edges")
        incoming = _edges(predecessor, "incoming_edges")
        dest = _current_expression(mlil, arm.dest_expr)
        src = _current_expression(mlil, arm.src_expr)
        if (
            predecessor_range is None
            or len(outgoing) != 1
            or len(incoming) != 1
            or type(arm.dest_addr) is not int
            or arm.dest_addr < 0
            or type(arm.src_addr) is not int
            or arm.src_addr < 0
            or not same_entity(getattr(edge, "source", None), predecessor)
            or not same_entity(getattr(edge, "target", None), join)
            or not same_entity(outgoing[0], edge)
            or not same_entity(getattr(goto, "il_basic_block", None), predecessor)
            or goto_index != predecessor_range[1] - 1
            or getattr(goto, "dest", None) != join_range[0]
            or dest is None
            or src is None
            or not same_entity(getattr(dest, "il_basic_block", None), predecessor)
            or not same_entity(getattr(src, "il_basic_block", None), predecessor)
            or not _concrete_address(dest, arm.dest_addr)
            or not _concrete_address(src, arm.src_addr)
        ):
            return None
        entry = incoming[0]
        entry_head = getattr(entry, "source", None)
        if (
            entry_head is None
            or not same_entity(getattr(entry, "target", None), predecessor)
            or (head is not None and not same_entity(head, entry_head))
        ):
            return None
        head = entry_head
        arms.append((goto_index, plan.size, arm.dest_addr, arm.src_addr))
        arm_edges.append(edge)
        arm_entries.append(entry)

    if (
        len({arm[0] for arm in arms}) != 2
        or not _same_entities(join_edges, arm_edges)
        or head is None
    ):
        return None
    head_edges = _edges(head, "outgoing_edges")
    if not _same_entities(head_edges, arm_entries):
        return None
    return _SKIP if not _pure_join_prefix(mlil, join, store) else (store_index, arms)


def _validated_plans(mlil, plans):
    try:
        plans = tuple(plans or ())
    except TypeError:
        return None
    if any(not _typed_plan(plan) for plan in plans):
        return None
    stores = {}
    arms_by_goto = {}
    for plan in plans:
        try:
            validated = _validated_plan(mlil, plan)
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — provider witness boundary must fail closed.
            return None
        if validated is _SKIP:
            continue
        if validated is None:
            return None
        store_index, arms = validated
        if store_index in stores:
            return None
        stores[store_index] = plan.size
        for goto_index, size, dest, src in arms:
            if goto_index == store_index:
                return None
            arms_by_goto.setdefault(goto_index, []).append((store_index, size, dest, src))
    if set(stores) & set(arms_by_goto):
        return None
    for arms in arms_by_goto.values():
        arms.sort(key=lambda arm: arm[0])
    return stores, arms_by_goto, len(stores)


def _store_prelude(arms):
    def emit(new_mlil, goto):
        address_size = getattr(getattr(new_mlil, "arch", None), "address_size", None)
        if type(address_size) is not int or address_size <= 0:
            raise ValueError("MLIL architecture has no address size")
        loc = ILSourceLocation.from_instruction(goto)
        out = []
        for _store_index, size, dest, src in arms:
            src_pointer = new_mlil.const_pointer(address_size, src, loc)
            out.append(
                new_mlil.store(
                    size,
                    new_mlil.const_pointer(address_size, dest, loc),
                    new_mlil.load(size, src_pointer, loc),
                    loc,
                )
            )
        return tuple(out)

    return emit


def _nop_store(new_mlil, store):
    return new_mlil.nop(ILSourceLocation.from_instruction(store))


def apply_correlated_stores_mlil(ctx, mlil, plans):
    """Copy MLIL, move each correlated store into its predecessor arm, and NOP its join."""
    if mlil is None:
        return None, 0
    validated = _validated_plans(mlil, plans)
    if validated is None:
        return None, 0
    stores, arms_by_goto, plan_count = validated
    if not plan_count:
        return mlil, 0
    replacements = {index: _nop_store for index in sorted(stores)}
    preludes = {index: _store_prelude(arms_by_goto[index]) for index in sorted(arms_by_goto)}
    expected = len(set(replacements) | set(preludes))
    new_mlil, copied = copy_mlil_with_instruction_rewrites(
        ctx,
        replacements,
        mlil=mlil,
        preludes=preludes,
    )
    return (new_mlil, plan_count) if copied == expected else (None, 0)
