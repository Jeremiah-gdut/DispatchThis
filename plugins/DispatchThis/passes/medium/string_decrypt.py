"""MLIL-stage string decrypt recognizer for the current sample family."""

from ...helpers.mlil import (
    CONST_OPS,
    LOAD_OPS,
    STORE_OPS,
    expression_or_definitions_have_operation,
    expression_scalar_value,
    iter_calls,
    op_name,
    peel_var_definitions,
    walk_expr,
    walk_expr_with_defs,
)
from ...utils.log import log_debug, log_info, log_warn


_CMP_OPS = (
    "MLIL_CMP_E",
    "MLIL_CMP_NE",
    "MLIL_CMP_ULT",
    "MLIL_CMP_SLT",
    "MLIL_CMP_ULE",
    "MLIL_CMP_SLE",
)
_MOD_OPS = ("MLIL_MODU", "MLIL_MODS", "MLIL_REMU", "MLIL_REMS")
_DIV_OPS = ("MLIL_DIVU", "MLIL_DIVS")
_MUL_OPS = ("MLIL_MUL",)


def _const(mlil, expr):
    # Preserve backend first-definition peeling; helper owns scalar extraction.
    expr = peel_var_definitions(mlil, expr, max_depth=32, allowed_ops=None)
    return expression_scalar_value(None, expr)


def _loads(expr):
    return [child for child in walk_expr(expr) if op_name(child) in LOAD_OPS]


def _mod_constants(mlil):
    out = set()
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if op_name(expr) not in _MOD_OPS:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                value = _const(mlil, side)
                if value and 0 < value <= 256:
                    out.add(value)
    return sorted(out)


def _divisor_constants(mlil):
    out = set()
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if op_name(expr) not in _DIV_OPS:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                value = _const(mlil, side)
                if value and 1 < value <= 256:
                    out.add(value)
    return sorted(out)


def _key_modulus_constants(mlil):
    return sorted(set(_mod_constants(mlil)) | set(_divisor_constants(mlil)))


def _length_constants(mlil, key_modulus):
    out = set()
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if op_name(expr) not in _CMP_OPS:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                value = _const(mlil, side)
                if value and value != key_modulus and 0 < value <= 4096:
                    out.add(value)
    return sorted(out)


def _parameters(func, mlil):
    for owner in (func, getattr(mlil, "source_function", None)):
        params = getattr(owner, "parameter_vars", None)
        if params:
            return list(params)
    return []


def _has_const(expr, value):
    return any(op_name(child) in CONST_OPS and child.constant == value for child in walk_expr(expr))


def _has_remainder_index(mlil, expr, key_modulus):
    expanded = list(walk_expr_with_defs(mlil, expr))
    if any(op_name(child) in _MOD_OPS and _has_const(child, key_modulus) for child in expanded):
        return True
    return (
        any(op_name(child) == "MLIL_SUB" for child in expanded)
        and any(op_name(child) in _DIV_OPS and _has_const(child, key_modulus) for child in expanded)
        and any(op_name(child) in _MUL_OPS and _has_const(child, key_modulus) for child in expanded)
    )


def _has_sample_family_loads(mlil, key_modulus):
    byte_loads = 0
    key_load = False
    for ins in getattr(mlil, "instructions", ()) or ():
        for load in _loads(ins):
            if getattr(load, "size", None) != 1:
                continue
            byte_loads += 1
            key_load = _has_remainder_index(mlil, getattr(load, "src", None), key_modulus) or key_load
    return key_load and byte_loads >= 2


def _has_byte_write_from_xor(mlil):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if op_name(expr) not in STORE_OPS or getattr(expr, "size", None) != 1:
                continue
            if expression_or_definitions_have_operation(mlil, getattr(expr, "src", None), "MLIL_XOR"):
                return True
    return False


def _has_done_flag_store(mlil):
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in walk_expr(ins):
            if op_name(expr) in STORE_OPS and _const(mlil, getattr(expr, "src", None)) == 1:
                return True
    return False


def recognize_string_decrypt_function(func, mlil=None):
    """Return decode parameters for one recognized string decrypt function."""
    mlil = mlil or getattr(func, "mlil", None) or getattr(func, "medium_level_il", None)
    if mlil is None:
        return None
    params = _parameters(func, mlil)
    if len(params) < 2:
        return None

    moduli = _key_modulus_constants(mlil)
    if not moduli:
        return None
    for key_modulus in moduli:
        lengths = _length_constants(mlil, key_modulus)
        if (
            lengths
            and _has_sample_family_loads(mlil, key_modulus)
            and _has_byte_write_from_xor(mlil)
            and _has_done_flag_store(mlil)
        ):
            return {"key_modulus": key_modulus, "length": lengths[-1]}
    name = getattr(func, "name", hex(getattr(func, "start", 0)))
    log_debug(f"[sdecrypt] {name}: shape mismatch")
    return None


def decode_string_blob(bv, source_addr, spec):
    key_modulus = spec["key_modulus"]
    length = spec["length"]
    data = bv.read(source_addr, key_modulus + length)
    if len(data) < key_modulus + length:
        return None
    key = data[:key_modulus]
    payload = data[key_modulus:key_modulus + length]
    out = bytearray()
    prev = 0
    for i, enc in enumerate(payload):
        k = key[i % key_modulus]
        if (((i % key_modulus) * k) & 1) == 0:
            tmp = ((prev + enc) & 0xFF) ^ ((~k) & 0xFF)
        else:
            tmp = (-(((enc - prev) & 0xFF) ^ k)) & 0xFF
        ch = tmp ^ k
        out.append(ch)
        prev = ch
    return bytes(out)


def _escaped(data):
    """Format plaintext for a single-line decrypt comment.

    ASCII controls and quotes stay C-style escaped. Valid UTF-8 printable text
    (including CJK and emoji) is shown as characters; invalid UTF-8 bytes stay
    as ``\\xHH``.
    """
    data = bytes(data)
    # surrogateescape keeps undecodable bytes recoverable as U+DC80..U+DCFF.
    text = data.decode("utf-8", errors="surrogateescape")
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0:
            out.append("\\0")
        elif code == 9:
            out.append("\\t")
        elif code == 10:
            out.append("\\n")
        elif code == 13:
            out.append("\\r")
        elif ch in ('"', "\\"):
            out.append("\\" + ch)
        elif 32 <= code <= 126:
            out.append(ch)
        elif code < 32 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        elif 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif ch.isprintable():
            out.append(ch)
        else:
            for byte in ch.encode("utf-8"):
                out.append(f"\\x{byte:02x}")
    return "".join(out)


def _comment_line(text, source_addr, dest_addr):
    return f"[decrypt] {text}, src={hex(source_addr)} dst={hex(dest_addr)}"


def _set_decrypt_comment(func, addr, line):
    get_comment_at = getattr(func, "get_comment_at", None)
    set_comment_at = getattr(func, "set_comment_at", None)
    if get_comment_at is None or set_comment_at is None:
        log_debug(f"[sdecrypt] {hex(addr)}: skipped missing function comment API")
        return False
    old = get_comment_at(addr) or ""
    new_lines = []
    replaced = False
    for item in old.splitlines():
        if item.startswith("[decrypt] "):
            if not replaced:
                new_lines.append(line)
                replaced = True
            continue
        new_lines.append(item)
    if not replaced:
        new_lines.append(line)
    new = "\n".join(new_lines)
    if new == old:
        return False
    set_comment_at(addr, new)
    return True


def plan_string_decrypt_calls(bv, _func, mlil, mlil_stable):
    if mlil is None:
        return []
    mlil_stable = mlil_stable or {}
    facts = []
    for call in iter_calls(mlil, ("MLIL_CALL", "MLIL_CALL_SSA", "MLIL_CALL_UNTYPED")):
        target = _const(mlil, getattr(call, "dest", None))
        if target is None:
            log_debug(f"[sdecrypt] {hex(call.address)}: skipped unresolved indirect call")
            continue
        params = list(getattr(call, "params", ()) or ())
        if len(params) < 2:
            log_debug(f"[sdecrypt] {hex(call.address)}: skipped fewer than two arguments")
            continue
        dest_addr = _const(mlil, params[0])
        source_addr = _const(mlil, params[1])
        if dest_addr is None or source_addr is None:
            log_debug(f"[sdecrypt] {hex(call.address)}: skipped non-constant source/dest")
            continue
        callee = bv.get_function_at(target)
        if callee is None or not mlil_stable.get(getattr(callee, "start", None)):
            log_debug(f"[sdecrypt] {hex(call.address)}: skipped non-stable callee {hex(target)}")
            continue
        spec = recognize_string_decrypt_function(callee)
        if spec is None:
            log_debug(f"[sdecrypt] {hex(call.address)}: skipped unrecognized callee {hex(target)}")
            continue
        plaintext = decode_string_blob(bv, source_addr, spec)
        if plaintext is None:
            log_warn(f"[sdecrypt] {hex(call.address)}: source blob @ {hex(source_addr)} is too short")
            continue
        facts.append({
            "call_addr": call.address,
            "src_addr": source_addr,
            "dst_addr": dest_addr,
            "plaintext": plaintext,
        })
    return facts


def apply_decrypted_string_comments(func, facts):
    changed = 0
    for fact in facts:
        line = _comment_line(_escaped(fact["plaintext"]), fact["src_addr"], fact["dst_addr"])
        if _set_decrypt_comment(func, fact["call_addr"], line):
            changed += 1
            log_info(f"[sdecrypt] {hex(fact['call_addr'])}: {line}")
    return changed


def annotate_decrypted_string_calls(bv, func, mlil):
    mlil_stable = bv.session_data.get("dispatchthis_mlil_stable", {})
    facts = plan_string_decrypt_calls(bv, func, mlil, mlil_stable)
    return apply_decrypted_string_comments(func, facts)
