"""MLIL-stage string decrypt recognizer for the current sample family."""

from ...utils.log import log_debug, log_info, log_warn


_CALL_OPS = ("MLIL_CALL", "MLIL_CALL_SSA", "MLIL_CALL_UNTYPED")
_CONST_OPS = ("MLIL_CONST", "MLIL_CONST_PTR")
_LOAD_OPS = ("MLIL_LOAD", "MLIL_LOAD_SSA", "MLIL_LOAD_STRUCT", "MLIL_LOAD_STRUCT_SSA")
_STORE_OPS = ("MLIL_STORE", "MLIL_STORE_SSA", "MLIL_STORE_STRUCT", "MLIL_STORE_STRUCT_SSA")
_CMP_OPS = (
    "MLIL_CMP_E",
    "MLIL_CMP_NE",
    "MLIL_CMP_ULT",
    "MLIL_CMP_SLT",
    "MLIL_CMP_ULE",
    "MLIL_CMP_SLE",
)
_MOD_OPS = ("MLIL_MODU", "MLIL_MODS", "MLIL_REMU", "MLIL_REMS")


def _op(expr):
    return getattr(getattr(expr, "operation", None), "name", None)


def _children(expr):
    for name in ("src", "dest", "left", "right", "condition"):
        child = getattr(expr, name, None)
        if hasattr(child, "operation"):
            yield child
    for name in ("params", "output", "vars_read", "vars_written"):
        for child in getattr(expr, name, ()) or ():
            if hasattr(child, "operation"):
                yield child


def _walk_exprs(expr, seen=None):
    if expr is None:
        return
    if seen is None:
        seen = set()
    key = id(expr)
    if key in seen:
        return
    seen.add(key)
    yield expr
    for child in _children(expr):
        yield from _walk_exprs(child, seen)


def _peel_var(mlil, expr):
    for _ in range(32):
        if _op(expr) != "MLIL_VAR":
            return expr
        try:
            defs = mlil.get_var_definitions(expr.src)
        except Exception:  # noqa: BLE001
            return expr
        if not defs or not hasattr(defs[0], "src"):
            return expr
        expr = defs[0].src
    return expr


def _const(mlil, expr):
    expr = _peel_var(mlil, expr)
    if _op(expr) in _CONST_OPS:
        return expr.constant
    value = getattr(expr, "value", None)
    value_type = getattr(getattr(value, "type", None), "name", None)
    if value_type in ("ConstantValue", "ConstantPointerValue", "ImportedAddressValue"):
        return value.value
    return None


def _contains_var(expr, var):
    return any(_op(child) == "MLIL_VAR" and child.src == var for child in _walk_exprs(expr))


def _loads(expr):
    return [child for child in _walk_exprs(expr) if _op(child) in _LOAD_OPS]


def _mod_constants(mlil):
    out = set()
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in _walk_exprs(ins):
            if _op(expr) not in _MOD_OPS:
                continue
            for side in (getattr(expr, "left", None), getattr(expr, "right", None)):
                value = _const(mlil, side)
                if value and 0 < value <= 256:
                    out.add(value)
    return sorted(out)


def _length_constants(mlil, key_modulus):
    out = set()
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in _walk_exprs(ins):
            if _op(expr) not in _CMP_OPS:
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


def _has_source_load_pair(expr, src_param, key_modulus):
    key_load = False
    payload_load = False
    for load in _loads(expr):
        addr = getattr(load, "src", None)
        if not _contains_var(addr, src_param):
            continue
        has_mod = any(
            _op(child) in _MOD_OPS
            and any(
                _const(None, side) == key_modulus
                for side in (getattr(child, "left", None), getattr(child, "right", None))
            )
            for child in _walk_exprs(addr)
        )
        if has_mod:
            key_load = True
        if any(
            _op(child) in _CONST_OPS and child.constant == key_modulus
            for child in _walk_exprs(addr)
        ):
            payload_load = True
    return key_load and payload_load


def recognize_string_decrypt_function(func, mlil=None):
    """Return decode parameters for one recognized string decrypt function."""
    mlil = mlil or getattr(func, "mlil", None) or getattr(func, "medium_level_il", None)
    if mlil is None:
        return None
    params = _parameters(func, mlil)
    if len(params) < 2:
        return None
    dest_param, src_param = params[:2]

    moduli = _mod_constants(mlil)
    if not moduli:
        return None
    key_modulus = moduli[0]
    lengths = _length_constants(mlil, key_modulus)
    if not lengths:
        return None
    length = lengths[-1]

    byte_write = False
    done_flag = False
    for ins in getattr(mlil, "instructions", ()) or ():
        for expr in _walk_exprs(ins):
            if _op(expr) not in _STORE_OPS:
                continue
            dest = getattr(expr, "dest", None)
            src = getattr(expr, "src", None)
            if _contains_var(dest, dest_param) and _op(src) == "MLIL_XOR":
                byte_write = _has_source_load_pair(src, src_param, key_modulus) or byte_write
            elif not _contains_var(dest, dest_param) and _const(mlil, src) == 1:
                done_flag = True

    if not byte_write or not done_flag:
        name = getattr(func, "name", hex(getattr(func, "start", 0)))
        log_debug(f"[sdecrypt] {name}: shape mismatch")
        return None
    return {"key_modulus": key_modulus, "length": length}


def decode_string_blob(bv, source_addr, spec):
    key_modulus = spec["key_modulus"]
    length = spec["length"]
    data = bv.read(source_addr, key_modulus + length)
    if len(data) < key_modulus + length:
        return None
    key = data[:key_modulus]
    payload = data[key_modulus:key_modulus + length]
    return bytes(ch ^ key[i % key_modulus] for i, ch in enumerate(payload))


def _escaped(data):
    out = []
    for ch in data:
        if ch == 0:
            out.append("\\0")
        elif ch == 9:
            out.append("\\t")
        elif ch == 10:
            out.append("\\n")
        elif ch == 13:
            out.append("\\r")
        elif ch in (34, 92):
            out.append("\\" + chr(ch))
        elif 32 <= ch <= 126:
            out.append(chr(ch))
        else:
            out.append(f"\\x{ch:02x}")
    return "".join(out)


def _direct_calls(mlil):
    for ins in getattr(mlil, "instructions", ()) or ():
        if _op(ins) in _CALL_OPS:
            yield ins


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


def annotate_decrypted_string_calls(bv, func, mlil):
    if mlil is None:
        return 0
    mlil_stable = bv.session_data.get("dispatchthis_mlil_stable", {})
    changed = 0
    for call in _direct_calls(mlil):
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
        line = _comment_line(_escaped(plaintext), source_addr, dest_addr)
        if _set_decrypt_comment(func, call.address, line):
            changed += 1
            log_info(f"[sdecrypt] {hex(call.address)}: {line}")
    return changed
