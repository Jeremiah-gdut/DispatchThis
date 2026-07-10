"""Recovery fact builders for resolver profiles."""


class MalformedRecoveryFact(ValueError):
    pass


def _require_int(name, value):
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedRecoveryFact(f"{name} must be an integer")
    return value


def _require_object(name, value):
    if value is None:
        raise MalformedRecoveryFact(f"{name} is required")
    return value


def _require_text(name, value):
    if not isinstance(value, str) or not value:
        raise MalformedRecoveryFact(f"{name} must be a non-empty string")
    return value


def _targets(values):
    try:
        targets = tuple(sorted(set(_require_int("targets", value) for value in values)))
    except TypeError as exc:
        raise MalformedRecoveryFact("targets must be an iterable of integers") from exc
    if not targets:
        raise MalformedRecoveryFact("targets must contain at least one address")
    return targets


def _int_set(name, values):
    if values is None:
        return set()
    try:
        return {_require_int(name, value) for value in values}
    except TypeError as exc:
        raise MalformedRecoveryFact(f"{name} must be an iterable of integers") from exc


def branch_fact(source, dest_expr_index, targets, newly_resolved=True, cleanup_roots=None):
    fact = {
        "source": _require_int("source", source),
        "dest_expr_index": _require_int("dest_expr_index", dest_expr_index),
        "targets": _targets(targets),
        "newly_resolved": bool(newly_resolved),
    }
    if cleanup_roots is not None:
        fact["cleanup_roots"] = _int_set("cleanup_roots", cleanup_roots)
    return fact


def call_fact(call_il, target, decode_def=None, cleanup_roots=None, call_addr=None):
    call_il = _require_object("call_il", call_il)
    if call_addr is None:
        call_addr = getattr(call_il, "address", None)
    return {
        "call_il": call_il,
        "call_addr": _require_int("call_addr", call_addr),
        "target": _require_int("target", target),
        "decode_def": decode_def,
        "cleanup_roots": _int_set("cleanup_roots", cleanup_roots),
    }


def global_constant_fact(slot_addr, type_name):
    return {
        "slot_addr": _require_int("slot_addr", slot_addr),
        "type": _require_text("type", type_name),
    }


def string_decrypt_fact(call_addr, src_addr, dst_addr, plaintext):
    if not isinstance(plaintext, (bytes, bytearray)):
        raise MalformedRecoveryFact("plaintext must be bytes")
    return {
        "call_addr": _require_int("call_addr", call_addr),
        "src_addr": _require_int("src_addr", src_addr),
        "dst_addr": _require_int("dst_addr", dst_addr),
        "plaintext": bytes(plaintext),
    }


__all__ = (
    "MalformedRecoveryFact",
    "branch_fact",
    "call_fact",
    "global_constant_fact",
    "string_decrypt_fact",
)
