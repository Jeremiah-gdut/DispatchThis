"""Identity-safe Binary Ninja wrapper and definition helpers."""

from __future__ import annotations


def entity_key(value):
    try:
        hash(value)
    except TypeError:
        return ("identity", id(value))
    return ("equality", value)


def same_entity(left, right):
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja wrapper equality boundary.
        return False


def expression_key(expression):
    owner = getattr(expression, "function", None)
    index = getattr(expression, "expr_index", None)
    if owner is not None and type(index) is int and index >= 0:
        return ("il", entity_key(owner), index)
    return ("identity", id(expression))


def variable_key(variable):
    version = getattr(variable, "version", None)
    if type(version) is int:
        for attribute in ("var", "reg"):
            base = getattr(variable, attribute, None)
            if base is not None:
                return ("ssa", entity_key(base), version)
    return ("variable", entity_key(variable))


def ssa_definition(il, variable):
    for name in ("get_ssa_reg_definition", "get_ssa_var_definition"):
        getter = getattr(il, name, None)
        if getter is None:
            continue
        try:
            definition = getter(variable)
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja dataflow query boundary.
            continue
        if definition is not None:
            return definition
    return None


def non_ssa_definitions(il, variable):
    getter = getattr(il, "get_var_definitions", None)
    if getter is None:
        return ()
    try:
        return tuple(getter(variable) or ())
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK — Binary Ninja dataflow query boundary.
        return ()
