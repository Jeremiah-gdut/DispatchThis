"""BNIL operation catalog and expression-shape helpers."""

from __future__ import annotations

from binaryninja import LowLevelILOperation as L, MediumLevelILOperation as M


def _names(*operations):
    return frozenset(
        operation.name for operation in operations if operation is not None
    )


def _all_names(*operation_types):
    return frozenset(
        operation.name
        for operation_type in operation_types
        for operation in getattr(operation_type, "__members__", {}).values()
    )


KNOWN_BNIL_OPERATIONS = _all_names(L, M)
CONSTANTS = _names(
    getattr(L, "LLIL_CONST", None),
    getattr(L, "LLIL_CONST_PTR", None),
    getattr(M, "MLIL_CONST", None),
    getattr(M, "MLIL_CONST_PTR", None),
)
SSA_VARIABLES = _names(
    getattr(L, "LLIL_REG_SSA", None),
    getattr(M, "MLIL_VAR_SSA", None),
)
PARTIAL_SSA_VARIABLES = _names(getattr(L, "LLIL_REG_SSA_PARTIAL", None))
NON_SSA_VARIABLES = _names(getattr(M, "MLIL_VAR", None))
PHIS = _names(getattr(L, "LLIL_REG_PHI", None), getattr(M, "MLIL_VAR_PHI", None))
SETS = _names(
    getattr(L, "LLIL_SET_REG_SSA", None),
    getattr(M, "MLIL_SET_VAR", None),
    getattr(M, "MLIL_SET_VAR_SSA", None),
)
CASTS = _names(
    getattr(L, "LLIL_ZX", None),
    getattr(L, "LLIL_SX", None),
    getattr(L, "LLIL_LOW_PART", None),
    getattr(M, "MLIL_ZX", None),
    getattr(M, "MLIL_SX", None),
    getattr(M, "MLIL_LOW_PART", None),
)
SIGNED_CASTS = _names(getattr(L, "LLIL_SX", None), getattr(M, "MLIL_SX", None))
NEGATIONS = _names(getattr(L, "LLIL_NEG", None), getattr(M, "MLIL_NEG", None))
NOTS = _names(getattr(L, "LLIL_NOT", None), getattr(M, "MLIL_NOT", None))
BOOL_TO_INT = _names(
    getattr(L, "LLIL_BOOL_TO_INT", None),
    getattr(M, "MLIL_BOOL_TO_INT", None),
)
BINARY_OPERATIONS = _names(
    *(
        getattr(operation, name, None)
        for operation in (L, M)
        for name in (
            "LLIL_ADD",
            "MLIL_ADD",
            "LLIL_SUB",
            "MLIL_SUB",
            "LLIL_MUL",
            "MLIL_MUL",
            "LLIL_AND",
            "MLIL_AND",
            "LLIL_OR",
            "MLIL_OR",
            "LLIL_XOR",
            "MLIL_XOR",
            "LLIL_LSL",
            "MLIL_LSL",
            "LLIL_LSR",
            "MLIL_LSR",
            "LLIL_ASR",
            "MLIL_ASR",
            "LLIL_ROL",
            "MLIL_ROL",
            "LLIL_ROR",
            "MLIL_ROR",
            "LLIL_DIVU",
            "MLIL_DIVU",
            "LLIL_DIVS",
            "MLIL_DIVS",
            "LLIL_MODU",
            "MLIL_MODU",
            "LLIL_MODS",
            "MLIL_MODS",
            "LLIL_TEST_BIT",
            "MLIL_TEST_BIT",
            "LLIL_ADD_OVERFLOW",
            "MLIL_ADD_OVERFLOW",
        )
    )
)
TERNARY_OPERATIONS = _names(
    *(
        getattr(operation, name, None)
        for operation in (L, M)
        for name in ("LLIL_ADC", "MLIL_ADC", "LLIL_SBB", "MLIL_SBB")
    )
)
COMPARISONS = _names(
    *(
        getattr(operation, name, None)
        for operation in (L, M)
        for name in (
            "LLIL_CMP_E",
            "MLIL_CMP_E",
            "LLIL_CMP_NE",
            "MLIL_CMP_NE",
            "LLIL_CMP_SLT",
            "MLIL_CMP_SLT",
            "LLIL_CMP_SLE",
            "MLIL_CMP_SLE",
            "LLIL_CMP_SGT",
            "MLIL_CMP_SGT",
            "LLIL_CMP_SGE",
            "MLIL_CMP_SGE",
            "LLIL_CMP_ULT",
            "MLIL_CMP_ULT",
            "LLIL_CMP_ULE",
            "MLIL_CMP_ULE",
            "LLIL_CMP_UGT",
            "MLIL_CMP_UGT",
            "LLIL_CMP_UGE",
            "MLIL_CMP_UGE",
        )
    )
)
SIGNED_COMPARISONS = frozenset(name for name in COMPARISONS if "_CMP_S" in name)
UNSUPPORTED_STANDARD_OPERATIONS = _names(
    *(
        getattr(operation, name, None)
        for operation in (L, M)
        for name in (
            "LLIL_RLC",
            "MLIL_RLC",
            "LLIL_RRC",
            "MLIL_RRC",
            "LLIL_MULU_DP",
            "MLIL_MULU_DP",
            "LLIL_MULS_DP",
            "MLIL_MULS_DP",
            "LLIL_DIVU_DP",
            "MLIL_DIVU_DP",
            "LLIL_DIVS_DP",
            "MLIL_DIVS_DP",
            "LLIL_MODU_DP",
            "MLIL_MODU_DP",
            "LLIL_MODS_DP",
            "MLIL_MODS_DP",
        )
    )
)
CONTROLLED_LOADS = _names(
    getattr(L, "LLIL_LOAD", None),
    getattr(L, "LLIL_LOAD_SSA", None),
    getattr(M, "MLIL_LOAD", None),
    getattr(M, "MLIL_LOAD_SSA", None),
    getattr(M, "MLIL_LOAD_STRUCT", None),
    getattr(M, "MLIL_LOAD_STRUCT_SSA", None),
)


def operation_name(expression):
    name = getattr(getattr(expression, "operation", None), "name", None)
    return name if type(name) is str else None


def is_expression(value):
    return getattr(value, "operation", None) is not None


def mask_for(expression):
    size = getattr(expression, "size", None)
    if type(size) is not int or size <= 0:
        return None
    return (1 << (size * 8)) - 1


def direct_operands(expression):
    operands = []
    for attribute in ("src", "left", "right", "carry", "condition", "params"):
        value = getattr(expression, attribute, None)
        if is_expression(value):
            operands.append(value)
        elif isinstance(value, (tuple, list)):
            operands.extend(item for item in value if is_expression(item))
    return tuple(operands)
