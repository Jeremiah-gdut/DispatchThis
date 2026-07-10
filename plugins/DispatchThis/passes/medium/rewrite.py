"""MLIL copy-transform helpers for medium pass control-flow rewrites."""

from binaryninja import ILSourceLocation, MediumLevelILFunction


def _instruction_index(ins, fallback):
    return getattr(ins, "instr_index", fallback)


def copied_label_for_source(mlil, instr_index):
    get_label = getattr(mlil, "get_label_for_source_instruction", None)
    if get_label is not None:
        label = get_label(instr_index)
        if label is not None:
            return label
    raise ValueError(f"no copied MLIL label for source instruction {instr_index}")


def copy_mlil_with_instruction_rewrites(ctx, replacements, mlil=None):
    """Copy MLIL and replace selected top-level instructions.

    ``replacements`` maps source instruction indices to callables receiving
    ``(new_mlil, old_instruction)`` and returning an expression for append().
    """
    old_mlil = mlil or getattr(ctx, "mlil", None)
    if old_mlil is None or not replacements:
        return old_mlil, 0

    try:
        new_mlil = MediumLevelILFunction(
            old_mlil.arch,
            low_level_il=getattr(ctx, "llil", getattr(old_mlil, "llil", None)),
        )
        new_mlil.prepare_to_copy_function(old_mlil)

        applied = 0
        for old_block in old_mlil.basic_blocks:
            new_mlil.prepare_to_copy_block(old_block)
            for idx in range(old_block.start, old_block.end):
                old_ins = old_mlil[idx]
                new_mlil.set_current_address(
                    old_ins.address,
                    getattr(old_block, "arch", None),
                )
                loc = ILSourceLocation.from_instruction(old_ins)
                rewrite = replacements.get(_instruction_index(old_ins, idx))
                if rewrite is None:
                    expr = old_ins.copy_to(new_mlil)
                else:
                    expr = rewrite(new_mlil, old_ins)
                    applied += 1
                new_mlil.append(expr, loc)

        if applied != len(replacements):
            return old_mlil, 0
        new_mlil.finalize()
        new_mlil.generate_ssa_form()
    except Exception:  # noqa: BLE001
        return old_mlil, 0
    return new_mlil, applied
