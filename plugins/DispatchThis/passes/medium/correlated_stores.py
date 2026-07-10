"""Apply validated path-correlated global-store recovery plans."""

from collections.abc import Mapping

from binaryninja import ILSourceLocation

from .rewrite import copy_mlil_with_instruction_rewrites


def _instruction_index(instruction):
    if type(instruction) is int:
        return instruction if instruction >= 0 else None
    index = getattr(instruction, "instr_index", None)
    return index if type(index) is int and index >= 0 else None


def _address(value):
    return value if type(value) is int and value >= 0 else None


def _validated_plans(plans):
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
        store_index = _instruction_index(plan.get("store"))
        size = plan.get("size")
        arms = plan.get("arms")
        if (
            store_index is None
            or store_index in stores
            or type(size) is not int
            or size <= 0
            or not isinstance(arms, (list, tuple))
            or len(arms) < 2
        ):
            return None
        stores[store_index] = size
        for arm in arms:
            if not isinstance(arm, Mapping):
                return None
            goto_index = _instruction_index(arm.get("goto"))
            dest = _address(arm.get("dest"))
            src = _address(arm.get("src"))
            if goto_index is None or goto_index == store_index or dest is None or src is None:
                return None
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

    validated = _validated_plans(plans)
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
