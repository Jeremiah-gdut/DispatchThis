"""Apply validated path-correlated global-store recovery plans."""

from collections.abc import Mapping

from binaryninja import ILSourceLocation, MediumLevelILOperation as M

from ...helpers.mlil import current_non_ssa_instruction, operation

from .rewrite import copy_mlil_with_instruction_rewrites


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
    values = tuple(
        getattr(expected, name, None)
        for name in ("instr_index", "expr_index", "address")
    )
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


def _address(value):
    return value if type(value) is int and value >= 0 else None


def _validated_plans(mlil, plans):
    try:
        plans = tuple(plans or ())
    except TypeError:
        return None
    if not plans:
        return ()

    stores = {}
    arms_by_goto = {}
    for plan in plans:
        if not isinstance(plan, Mapping):
            return None
        store = _current_instruction(mlil, plan.get("store"), M.MLIL_STORE)
        store_index = getattr(store, "instr_index", None)
        size = plan.get("size")
        arms = plan.get("arms")
        if (
            store_index is None
            or store_index in stores
            or type(size) is not int
            or size <= 0
            or getattr(store, "size", None) != size
            or not isinstance(arms, (list, tuple))
            or len(arms) != 2
        ):
            return None
        stores[store_index] = size
        plan_gotos = set()
        for arm in arms:
            if not isinstance(arm, Mapping):
                return None
            goto = _current_instruction(mlil, arm.get("goto"), M.MLIL_GOTO)
            goto_index = getattr(goto, "instr_index", None)
            dest = _address(arm.get("dest"))
            src = _address(arm.get("src"))
            if (
                goto_index is None
                or goto_index == store_index
                or goto_index in plan_gotos
                or dest is None
                or src is None
            ):
                return None
            plan_gotos.add(goto_index)
            arms_by_goto.setdefault(goto_index, []).append((size, dest, src))

    if set(stores) & set(arms_by_goto):
        return None
    return stores, arms_by_goto, len(plans)


def _store_prelude(arms):
    def emit(new_mlil, goto):
        address_size = getattr(getattr(new_mlil, "arch", None), "address_size", None)
        if type(address_size) is not int or address_size <= 0:
            raise ValueError("MLIL architecture has no address size")
        loc = ILSourceLocation.from_instruction(goto)
        out = []
        for size, dest, src in arms:
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
    if not validated:
        return mlil, 0

    stores, arms_by_goto, plan_count = validated
    replacements = {index: _nop_store for index in sorted(stores)}
    preludes = {
        index: _store_prelude(arms_by_goto[index])
        for index in sorted(arms_by_goto)
    }
    expected = len(set(replacements) | set(preludes))
    new_mlil, copied = copy_mlil_with_instruction_rewrites(
        ctx,
        replacements,
        mlil=mlil,
        preludes=preludes,
    )
    return (new_mlil, plan_count) if copied == expected else (None, 0)
