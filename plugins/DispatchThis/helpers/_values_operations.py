"""Pure standard BNIL integer-operation evaluation."""

from __future__ import annotations

from ..semantics import Inconclusive
from ._values_bnil import (
    BINARY_OPERATIONS,
    BOOL_TO_INT,
    CASTS,
    COMPARISONS,
    NEGATIONS,
    NOTS,
    SIGNED_CASTS,
    SIGNED_COMPARISONS,
    TERNARY_OPERATIONS,
    UNSUPPORTED_STANDARD_OPERATIONS,
    mask_for,
)


def _signed(value, size):
    if type(size) is not int or size <= 0:
        return None
    bits = size * 8
    mask = (1 << bits) - 1
    value &= mask
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _signed_operands(expression, operands):
    sizes = tuple(
        getattr(getattr(expression, attribute, None), "size", None)
        for attribute in ("left", "right")
    )
    if any(type(size) is not int or size <= 0 for size in sizes):
        return None
    return tuple(_signed(value, size) for value, size in zip(operands, sizes))


def _signed_divide(left, right):
    quotient = abs(left) // abs(right)
    return -quotient if (left < 0) != (right < 0) else quotient


def standard_value(operation, expression, operands):
    """Return a standard value, Inconclusive, or None for a policy operation."""

    mask = mask_for(expression)
    if mask is None:
        return Inconclusive("expression width is unavailable")
    if operation in CASTS:
        if len(operands) != 1:
            return Inconclusive("cast operands are incomplete")
        value = operands[0]
        if operation in SIGNED_CASTS:
            source_size = getattr(getattr(expression, "src", None), "size", None)
            signed = _signed(value, source_size)
            if signed is None:
                return Inconclusive("signed cast source width is unavailable")
            value = signed
        return value & mask
    if operation in NEGATIONS:
        if len(operands) != 1:
            return Inconclusive("negation operands are incomplete")
        return (-operands[0]) & mask
    if operation in NOTS:
        if len(operands) != 1:
            return Inconclusive("not operands are incomplete")
        return (~operands[0]) & mask
    if operation in BOOL_TO_INT:
        if len(operands) != 1:
            return Inconclusive("boolean conversion operands are incomplete")
        return operands[0] & mask
    if operation in TERNARY_OPERATIONS:
        if len(operands) != 3:
            return Inconclusive("ternary operation operands are incomplete")
        left, right, carry = operands
        if operation.endswith("_ADC"):
            return (left + right + carry) & mask
        return (left - right - carry) & mask
    if operation in BINARY_OPERATIONS:
        if len(operands) != 2:
            return Inconclusive("binary operation operands are incomplete")
        return _binary_value(operation, expression, operands, mask)
    if operation in COMPARISONS:
        if len(operands) != 2:
            return Inconclusive("comparison operands are incomplete")
        return _comparison_value(operation, expression, operands)
    if operation in UNSUPPORTED_STANDARD_OPERATIONS:
        return Inconclusive(f"unsupported standard operation {operation}")
    return None


def _binary_value(operation, expression, operands, mask):
    left, right = operands
    bits = getattr(expression, "size", 0) * 8
    if operation.endswith("_ADD"):
        return (left + right) & mask
    if operation.endswith("_SUB"):
        return (left - right) & mask
    if operation.endswith("_MUL"):
        return (left * right) & mask
    if operation.endswith("_AND"):
        return (left & right) & mask
    if operation.endswith("_OR"):
        return (left | right) & mask
    if operation.endswith("_XOR"):
        return (left ^ right) & mask
    if operation.endswith("_LSL"):
        return 0 if right >= bits else (left << right) & mask
    if operation.endswith("_LSR"):
        return 0 if right >= bits else (left >> right) & mask
    if operation.endswith("_ASR"):
        signed = _signed(left, getattr(expression, "size", None))
        if signed is None:
            return Inconclusive("arithmetic shift width is unavailable")
        return (signed >> min(right, bits)) & mask
    if operation.endswith("_ROL"):
        shift = right % bits
        return ((left << shift) | (left >> (bits - shift))) & mask
    if operation.endswith("_ROR"):
        shift = right % bits
        return ((left >> shift) | (left << (bits - shift))) & mask
    if operation.endswith("_DIVU") or operation.endswith("_MODU"):
        if right == 0:
            return Inconclusive("unsigned division by zero")
        return (
            (left // right) if operation.endswith("_DIVU") else (left % right)
        ) & mask
    if operation.endswith("_DIVS") or operation.endswith("_MODS"):
        signed = _signed_operands(expression, operands)
        if signed is None:
            return Inconclusive("signed division width is unavailable")
        left, right = signed
        if right == 0:
            return Inconclusive("signed division by zero")
        quotient = _signed_divide(left, right)
        return (
            quotient if operation.endswith("_DIVS") else left - quotient * right
        ) & mask
    if operation.endswith("_TEST_BIT"):
        return 0 if right >= bits else (left >> right) & 1
    if operation.endswith("_ADD_OVERFLOW"):
        result = (left + right) & mask
        sign_bit = 1 << (bits - 1)
        return int(
            (left & sign_bit) == (right & sign_bit)
            and (result & sign_bit) != (left & sign_bit)
        )
    return Inconclusive(f"unsupported standard operation {operation}")


def _comparison_value(operation, expression, operands):
    left, right = operands
    if operation in SIGNED_COMPARISONS:
        signed = _signed_operands(expression, operands)
        if signed is None:
            return Inconclusive("signed comparison width is unavailable")
        left, right = signed
    if operation.endswith("_CMP_E"):
        return int(left == right)
    if operation.endswith("_CMP_NE"):
        return int(left != right)
    if operation.endswith("_CMP_SLT") or operation.endswith("_CMP_ULT"):
        return int(left < right)
    if operation.endswith("_CMP_SLE") or operation.endswith("_CMP_ULE"):
        return int(left <= right)
    if operation.endswith("_CMP_SGT") or operation.endswith("_CMP_UGT"):
        return int(left > right)
    return int(left >= right)
