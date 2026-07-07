"""MLIL profile helpers."""

from .memory import read_uint_le


U64 = 0xFFFFFFFFFFFFFFFF

CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
LOAD_STRUCT_OPS = ("MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")
LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", *LOAD_STRUCT_OPS)
SLOT_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", *LOAD_STRUCT_OPS)
SET_VAR_OPS = ("MLIL_SET_VAR", "MLIL_SET_VAR_FIELD")
STORE_OPS = ("MLIL_STORE", "MLIL_STORE_SSA", "MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA")


def walk_expr(expr):
    if expr is None:
        return []
    try:
        return list(expr.traverse(lambda node: node))
    except AttributeError:
        pass

    out = []
    seen = set()

    def visit(node):
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        out.append(node)
        for name in ("src", "dest", "left", "right", "condition"):
            child = getattr(node, name, None)
            if hasattr(child, "operation"):
                visit(child)
        for name in ("params", "output", "vars_read", "vars_written"):
            for child in getattr(node, name, ()) or ():
                if hasattr(child, "operation"):
                    visit(child)

    visit(expr)
    return out


def _op_name(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def _mask_address(value, address_mask):
    return value if address_mask is None else value & address_mask


def constant_value(mlil, expr):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    return expr.constant if _op_name(expr) in CONST_OPS else None


def constant_address(mlil, expr, depth=0, max_depth=32, address_mask=None):
    if expr is None or depth > max_depth:
        return None
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=max_depth,
        require_single=True,
        allowed_ops=None,
    )
    value = constant_value(mlil, expr)
    if value is not None:
        return _mask_address(value, address_mask)
    op = _op_name(expr)
    if op in ("MLIL_ADD", "MLIL_SUB"):
        left = constant_address(mlil, expr.left, depth + 1, max_depth, address_mask)
        right = constant_address(mlil, expr.right, depth + 1, max_depth, address_mask)
        if left is not None and right is not None:
            return _mask_address(left + right if op == "MLIL_ADD" else left - right, address_mask)
    return None


def load_slot_address(mlil, expr, width=8, address_mask=None):
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=32,
        require_single=True,
        allowed_ops=None,
    )
    op = _op_name(expr)
    if op not in SLOT_LOAD_OPS or getattr(expr, "size", None) != width:
        return None
    addr = constant_address(mlil, expr.src, address_mask=address_mask)
    if addr is None:
        return None
    if op in LOAD_STRUCT_OPS:
        offset = getattr(expr, "offset", 0)
        if not isinstance(offset, int):
            return None
        return _mask_address(addr + offset, address_mask)
    return addr


def mlil_stores_to_address(mlil, addr, address_mask=None):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if (
                _op_name(expr) in STORE_OPS
                and constant_address(
                    mlil,
                    getattr(expr, "dest", None),
                    address_mask=address_mask,
                ) == addr
            ):
                return True
    return False


def walk_expr_with_defs(mlil, expr, max_depth=16):
    """Yield expression nodes, following MLIL_VAR definitions."""
    yield from _walk_expr_with_defs(mlil, expr, set(), set(), 0, max_depth)


def _walk_expr_with_defs(mlil, expr, seen_exprs, seen_vars, depth, max_depth):
    if expr is None:
        return
    for child in walk_expr(expr):
        expr_key = id(child)
        if expr_key in seen_exprs:
            continue
        seen_exprs.add(expr_key)
        yield child
        if _op_name(child) != "MLIL_VAR" or mlil is None or depth >= max_depth:
            continue
        var_key = repr(getattr(child, "src", None))
        if var_key in seen_vars:
            continue
        seen_vars.add(var_key)
        try:
            definitions = mlil.get_var_definitions(child.src)
        except Exception:  # noqa: BLE001
            continue
        for definition in definitions or ():
            yield from _walk_expr_with_defs(
                mlil,
                getattr(definition, "src", None),
                seen_exprs,
                seen_vars,
                depth + 1,
                max_depth,
            )


def load_slot_offsets(mlil, expr, width=8, address_mask=None, max_depth=32):
    """Return ``(slot_addr, offset)`` pairs for slot loads plus constant offsets."""
    return _load_slot_offsets(mlil, expr, width, address_mask, 0, max_depth)


def iter_load_slot_offsets(mlil, width=8, address_mask=None):
    """Yield ``(expr, use_addr, slot_addr, offset)`` for every slot-offset use."""
    for ins in getattr(mlil, "instructions", ()) or ():
        ins_addr = getattr(ins, "address", 0)
        for expr in walk_expr(ins):
            use_addr = getattr(expr, "address", ins_addr)
            for slot_addr, offset in load_slot_offsets(
                mlil,
                expr,
                width=width,
                address_mask=address_mask,
            ):
                yield expr, use_addr, slot_addr, offset


def _load_slot_offsets(mlil, expr, width, address_mask, depth, max_depth):
    if depth > max_depth:
        return []
    expr = peel_var_definitions(
        mlil,
        expr,
        max_depth=max_depth,
        require_single=True,
        allowed_ops=None,
    )
    slot_addr = load_slot_address(mlil, expr, width=width, address_mask=address_mask)
    if slot_addr is not None:
        return [(slot_addr, 0)]

    op = _op_name(expr)
    if op not in ("MLIL_ADD", "MLIL_SUB"):
        return []

    out = []
    right_const = constant_value(mlil, expr.right)
    if right_const is not None:
        addend = _signed_offset(right_const)
        if op == "MLIL_SUB":
            addend = -addend
        out.extend(
            (slot, offset + addend)
            for slot, offset in _load_slot_offsets(
                mlil,
                expr.left,
                width,
                address_mask,
                depth + 1,
                max_depth,
            )
        )

    if op == "MLIL_ADD":
        left_const = constant_value(mlil, expr.left)
        if left_const is not None:
            addend = _signed_offset(left_const)
            out.extend(
                (slot, offset + addend)
                for slot, offset in _load_slot_offsets(
                    mlil,
                    expr.right,
                    width,
                    address_mask,
                    depth + 1,
                    max_depth,
                )
            )

    return out


def _signed_offset(value):
    return value - (1 << 64) if value > 0x7FFFFFFFFFFFFFFF else value


def iter_indirect_calls(mlil):
    """Yield MLIL call instructions whose destination is not already direct."""
    if mlil is None:
        return
    for insn in mlil.instructions:
        if not insn.operation.name.startswith("MLIL_CALL"):
            continue
        if insn.dest.operation.name in CONST_OPS:
            continue
        yield insn


def peel_var_definitions(
    mlil,
    expr,
    trail=None,
    max_depth=64,
    require_single=False,
    allowed_ops=SET_VAR_OPS,
):
    """Follow MLIL_VAR through SET_VAR definitions and return the peeled expr."""
    for _ in range(max_depth):
        if expr is None or expr.operation.name != "MLIL_VAR":
            break
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            break
        if not defs or (require_single and len(defs) != 1):
            break
        definition = defs[0]
        if allowed_ops is not None and definition.operation.name not in allowed_ops:
            break
        if not hasattr(definition, "src"):
            break
        if trail is not None:
            trail.append(definition)
        expr = definition.src
    return expr


def _single_value(expr):
    try:
        value = expr.value
    except Exception:  # noqa: BLE001
        return None
    if value.type.name in ("ConstantValue", "ConstantPointerValue", "ImportedAddressValue"):
        return value.value & U64
    return None


def fold_constant_value(bv, mlil, expr, depth=0, max_depth=32, load_address_mask=None):
    """Best-effort single-value fold for current MLIL call-target recovery."""
    if expr is None or depth > max_depth:
        return None
    op = expr.operation.name

    if op in CONST_OPS:
        return expr.constant & U64

    if op == "MLIL_VAR":
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            defs = ()
        if defs and defs[0].operation.name in SET_VAR_OPS:
            value = fold_constant_value(
                bv, mlil, defs[0].src, depth + 1, max_depth, load_address_mask
            )
            if value is not None:
                return value
        return _single_value(expr)

    if op in ("MLIL_ADD", "MLIL_SUB"):
        left = fold_constant_value(
            bv, mlil, expr.left, depth + 1, max_depth, load_address_mask
        )
        right = fold_constant_value(
            bv, mlil, expr.right, depth + 1, max_depth, load_address_mask
        )
        if left is None or right is None:
            return None
        return (left + right if op == "MLIL_ADD" else left - right) & U64

    if op == "MLIL_MUL":
        left = fold_constant_value(
            bv, mlil, expr.left, depth + 1, max_depth, load_address_mask
        )
        right = fold_constant_value(
            bv, mlil, expr.right, depth + 1, max_depth, load_address_mask
        )
        return None if left is None or right is None else (left * right) & U64

    if op in ("MLIL_ZX", "MLIL_SX", "MLIL_LOW_PART"):
        return fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth, load_address_mask)

    if op in LOAD_OPS:
        addr = fold_constant_value(bv, mlil, expr.src, depth + 1, max_depth, load_address_mask)
        if addr is None:
            return None
        return read_uint_le(bv, _mask_address(addr, load_address_mask), expr.size)

    return _single_value(expr)


def cleanup_roots_for_expr(mlil, expr):
    """Instruction indices defining MLIL vars read by ``expr``."""
    roots = set()
    for node in walk_expr(expr):
        if node.operation.name != "MLIL_VAR":
            continue
        try:
            defs = mlil.get_var_definitions(node.src)
        except Exception:  # noqa: BLE001
            continue
        for definition in defs:
            if definition.operation.name in SET_VAR_OPS:
                roots.add(definition.instr_index)
    return roots


def set_roots_before(mlil, site_addrs):
    """Contiguous pure assignment instruction indices before owned sites."""
    site_addrs = set(site_addrs or ())
    roots = set()
    if mlil is None or not site_addrs:
        return roots

    for block in mlil.basic_blocks:
        block_instrs = [mlil[i] for i in range(block.start, block.end)]
        for pos, ins in enumerate(block_instrs):
            if ins.address not in site_addrs:
                continue
            for prev in reversed(block_instrs[:pos]):
                if prev.operation.name not in SET_VAR_OPS:
                    break
                roots.add(prev.instr_index)
    return roots


__all__ = (
    "CONST_OPS",
    "LOAD_STRUCT_OPS",
    "LOAD_OPS",
    "SET_VAR_OPS",
    "SLOT_LOAD_OPS",
    "STORE_OPS",
    "cleanup_roots_for_expr",
    "constant_address",
    "constant_value",
    "fold_constant_value",
    "iter_load_slot_offsets",
    "iter_indirect_calls",
    "load_slot_offsets",
    "load_slot_address",
    "mlil_stores_to_address",
    "peel_var_definitions",
    "set_roots_before",
    "walk_expr",
    "walk_expr_with_defs",
)
