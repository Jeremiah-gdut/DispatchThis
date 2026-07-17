"""BinaryView memory and address helpers for resolver profiles."""

from binaryninja import SymbolType

from ..semantics import Inconclusive
from ._values_bnil import CONTROLLED_LOADS, operation_name
from ._values_contracts import Handled, NotHandled


_FUNCTION_SYMBOL_TYPES = {
    SymbolType.FunctionSymbol,
    SymbolType.ImportedFunctionSymbol,
    SymbolType.LibraryFunctionSymbol,
    SymbolType.SymbolicFunctionSymbol,
}
_INITIALIZED_DATA_SEMANTICS = frozenset(
    ("ReadOnlyDataSectionSemantics", "ReadWriteDataSectionSemantics")
)
_STATIC_LOAD_WIDTHS = frozenset((1, 2, 4, 8))


class InitializedDataPolicy:
    """Evaluate provider-approved loads from an immutable initialized-data image."""

    def __init__(self, byte_order, regions):
        self.byte_order = byte_order
        self._regions = regions

    def bytes_at(self, address, width):
        if (
            type(address) is not int
            or type(width) is not int
            or address < 0
            or width <= 0
        ):
            return None
        end = address + width
        if end <= address:
            return None
        for start, region_end, data in self._regions:
            if start <= address and end <= region_end:
                offset = address - start
                return data[offset : offset + width]
        return None

    def __call__(self, expression, operands):
        if operation_name(expression) not in CONTROLLED_LOADS:
            return NotHandled()
        if (
            len(operands) != 1
            or len(operands[0]) != 1
            or type(operands[0][0]) is not int
        ):
            return Inconclusive("static data load has an unexpected operand shape")
        width = getattr(expression, "size", None)
        if type(width) is not int or width not in _STATIC_LOAD_WIDTHS:
            return Inconclusive("static data load has an unsupported width")
        data = self.bytes_at(operands[0][0], width)
        if data is None:
            return Inconclusive("static data load is outside the initialized-data snapshot")
        return Handled((int.from_bytes(data, self.byte_order),))


def byte_order(bv):
    """Return the BinaryView byte order as ``"little"`` or ``"big"``."""

    endian = getattr(bv, "endianness", None)
    name = getattr(endian, "name", None)
    if name is None:
        name = getattr(getattr(bv, "arch", None), "endianness", None)
        name = getattr(name, "name", None)
    return {"LittleEndian": "little", "BigEndian": "big"}.get(name)


def initialized_data_policy(bv):
    """Snapshot initialized data for explicit, pure value-policy use."""

    order = byte_order(bv)
    if order is None:
        return None
    try:
        sections = tuple(getattr(bv, "sections", {}).values())
    except Exception:  # noqa: BLE001 - Binary Ninja wrapper boundary.
        return None
    regions = []
    for section in sections:
        semantics = getattr(getattr(section, "semantics", None), "name", None)
        if semantics not in _INITIALIZED_DATA_SEMANTICS:
            continue
        if getattr(section, "type", None) == "NOBITS":
            continue
        start = getattr(section, "start", None)
        end = getattr(section, "end", None)
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            continue
        try:
            raw = bv.read(start, end - start)
        except Exception:  # noqa: BLE001 - unreadable sections cannot prove a load.
            continue
        if raw is None:
            continue
        data = bytes(raw)
        if len(data) == end - start:
            regions.append((start, end, data))
    regions.sort(key=lambda region: region[0])
    if not regions or any(
        next_region[0] < region[1] for region, next_region in zip(regions, regions[1:])
    ):
        return None
    return InitializedDataPolicy(order, tuple(regions))


def read_uint_le(bv, addr, width):
    """Read an unsigned little-endian integer, or ``None`` on normal misses."""
    if width <= 0:
        raise ValueError("width must be positive")
    try:
        data = bv.read(addr, width)
    except Exception:  # noqa: BLE001
        return None
    if data is None or len(data) != width:
        return None
    return int.from_bytes(data, "little")


def read_u8(bv, addr):
    return read_uint_le(bv, addr, 1)


def read_u16le(bv, addr):
    return read_uint_le(bv, addr, 2)


def read_u32le(bv, addr):
    return read_uint_le(bv, addr, 4)


def read_u64le(bv, addr):
    return read_uint_le(bv, addr, 8)


def read_qword_slot(bv, addr):
    return read_u64le(bv, addr)


def is_mapped_address(bv, addr):
    """Return whether ``addr`` belongs to the BinaryView address space."""
    try:
        return addr is not None and bool(bv.is_valid_offset(addr))
    except Exception:  # noqa: BLE001
        return False


def is_executable_target(bv, addr):
    """Return whether ``addr`` is an aligned executable control-flow target."""
    if addr is None:
        return False
    try:
        alignment = getattr(getattr(bv, "arch", None), "instr_alignment", 1) or 1
        return addr % alignment == 0 and bool(bv.is_offset_executable(addr))
    except Exception:  # noqa: BLE001
        return False


def is_known_callee(bv, addr):
    """Return whether BN identifies ``addr`` as executable or function-like."""
    if not is_mapped_address(bv, addr):
        return False
    try:
        if bv.get_function_at(addr) is not None or is_executable_target(bv, addr):
            return True
        get_symbols = getattr(bv, "get_symbols", None)
        symbols = list(get_symbols(addr, 1) or ()) if get_symbols is not None else []
        if not symbols:
            symbol = bv.get_symbol_at(addr)
            symbols = [] if symbol is None else [symbol]
        return any(getattr(symbol, "type", None) in _FUNCTION_SYMBOL_TYPES for symbol in symbols)
    except Exception:  # noqa: BLE001
        return False


def sections_at(bv, addr):
    try:
        return tuple(bv.get_sections_at(addr) or ())
    except Exception:  # noqa: BLE001
        return ()


def in_section(bv, addr, names):
    if isinstance(names, str):
        names = {names}
    else:
        names = set(names)
    return any(getattr(section, "name", None) in names for section in sections_at(bv, addr))


__all__ = (
    "InitializedDataPolicy",
    "byte_order",
    "initialized_data_policy",
    "in_section",
    "is_executable_target",
    "is_known_callee",
    "is_mapped_address",
    "read_qword_slot",
    "read_u8",
    "read_u16le",
    "read_u32le",
    "read_u64le",
    "read_uint_le",
    "sections_at",
)
