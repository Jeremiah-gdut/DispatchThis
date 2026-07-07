"""BinaryView memory and address helpers for resolver profiles."""


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


def is_valid_address(bv, addr):
    try:
        return addr is not None and bool(bv.is_valid_offset(addr))
    except Exception:  # noqa: BLE001
        return False


def is_valid_target(bv, addr):
    return is_valid_address(bv, addr)


def is_call_target(bv, addr):
    if not is_valid_address(bv, addr):
        return False
    try:
        return bv.get_symbol_at(addr) is not None or bv.get_function_at(addr) is not None
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
    "in_section",
    "is_call_target",
    "is_valid_address",
    "is_valid_target",
    "read_qword_slot",
    "read_u8",
    "read_u16le",
    "read_u32le",
    "read_u64le",
    "read_uint_le",
    "sections_at",
)
