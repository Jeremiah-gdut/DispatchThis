from conftest import load_plugin_module


string_decrypt = load_plugin_module("plugins.DispatchThis.passes.medium.string_decrypt")

annotate_decrypted_string_calls = string_decrypt.annotate_decrypted_string_calls
apply_decrypted_string_comments = string_decrypt.apply_decrypted_string_comments
decode_string_blob = string_decrypt.decode_string_blob
plan_string_decrypt_calls = string_decrypt.plan_string_decrypt_calls
recognize_string_decrypt_function = string_decrypt.recognize_string_decrypt_function
_escaped = string_decrypt._escaped
_set_decrypt_comment = string_decrypt._set_decrypt_comment


class Op:
    def __init__(self, name):
        self.name = name


class Expr:
    def __init__(self, op, **attrs):
        self.operation = Op(op)
        self.__dict__.update(attrs)


class FakeMlil:
    def __init__(self, instructions, source_function=None, defs=None):
        self.instructions = instructions
        self.source_function = source_function
        self._defs = defs or {}
        for ins in instructions:
            if getattr(getattr(ins, "operation", None), "name", None) == "MLIL_SET_VAR":
                self._defs.setdefault(ins.dest, []).append(ins)

    def get_var_definitions(self, var):
        return self._defs.get(var, [])


class FakeFunc:
    def __init__(self, start=0x1000, mlil=None, parameter_vars=()):
        self.start = start
        self.mlil = mlil
        self.parameter_vars = list(parameter_vars)
        self.comments = {}
        if mlil is not None:
            mlil.source_function = self

    def get_comment_at(self, addr):
        return self.comments.get(addr, "")

    def set_comment_at(self, addr, text):
        self.comments[addr] = text


class FakeBv:
    def __init__(self):
        self.memory = {}
        self.functions = {}
        self.session_data = {"dispatchthis_mlil_stable": {}}

    def read(self, addr, size):
        data = self.memory.get(addr, b"")
        return data[:size]

    def get_function_at(self, addr):
        return self.functions.get(addr)

    def get_comment_at(self, *_args):
        raise AssertionError("decrypt comments must be function-level comments")

    def set_comment_at(self, *_args):
        raise AssertionError("decrypt comments must be function-level comments")


def const(value):
    return Expr("MLIL_CONST_PTR", constant=value)


def var(name):
    return Expr("MLIL_VAR", src=name)


def add(left, right):
    return Expr("MLIL_ADD", left=left, right=right)


def mod(left, right):
    return Expr("MLIL_MODU", left=left, right=right)


def xor(left, right):
    return Expr("MLIL_XOR", left=left, right=right)


def and_(left, right):
    return Expr("MLIL_AND", left=left, right=right)


def divu(left, right):
    return Expr("MLIL_DIVU", left=left, right=right)


def mul(left, right):
    return Expr("MLIL_MUL", left=left, right=right)


def sub(left, right):
    return Expr("MLIL_SUB", left=left, right=right)


def set_var(name, src, address=0x1000):
    return Expr("MLIL_SET_VAR", dest=name, src=src, address=address)


def load(src, size=1, op="MLIL_LOAD"):
    return Expr(op, src=src, size=size)


def store(dest, src, address=0x1000, size=1):
    return Expr("MLIL_STORE", dest=dest, src=src, address=address, size=size)


def cmp_ult(left, right):
    return Expr("MLIL_CMP_ULT", left=left, right=right)


def if_(condition):
    return Expr("MLIL_IF", condition=condition)


def call(dest, params, address=0x5000):
    return Expr("MLIL_CALL", dest=dest, params=list(params), address=address)


def encoded_blob(text, key=b"k3y!"):
    plain = text if isinstance(text, bytes) else text.encode("ascii")
    out = bytearray(key)
    prev = 0
    for i, ch in enumerate(plain):
        k = key[i % len(key)]
        if (((i % len(key)) * k) & 1) == 0:
            enc = ((((ch ^ k) ^ (~k & 0xFF)) - prev) & 0xFF)
        else:
            enc = (((-(ch ^ k) & 0xFF) ^ k) + prev) & 0xFF
        out.append(enc)
        prev = ch
    return bytes(out)


def decrypt_callee(text="libUE4.so", start=0x2000, key_modulus=4, load_op="MLIL_LOAD"):
    dest = "dst"
    src = "src"
    i = "i"
    mlil = FakeMlil(
        [
            if_(cmp_ult(var(i), const(len(text)))),
            set_var("q", divu(var(i), const(key_modulus))),
            set_var("q_scaled", mul(var("q"), const(key_modulus))),
            set_var("rem", sub(var(i), var("q_scaled"))),
            set_var("key_addr", add(var(src), var("rem"))),
            set_var("key_byte", load(var("key_addr"), op=load_op)),
            set_var("payload_addr", add(var("payload_base"), var(i))),
            set_var("enc_byte", load(var("payload_addr"), op=load_op)),
            set_var("parity", and_(mul(var("rem"), var("key_byte")), const(1))),
            if_(cmp_ult(var("parity"), const(1))),
            set_var("tmp", xor(add(var("prev"), var("enc_byte")), var("key_byte"))),
            set_var("tmp", xor(sub(var("enc_byte"), var("prev")), var("key_byte"))),
            set_var("out", xor(var("tmp"), var("key_byte"))),
            store(add(var(dest), var(i)), var("out")),
            store(const(0x9000), const(1)),
        ],
    )
    return FakeFunc(start, mlil, [dest, src])


def test_recognizer_matches_sample_family_decrypt_shape():
    spec = recognize_string_decrypt_function(decrypt_callee(b"libUE4.so\x00", key_modulus=16))

    assert spec == {"key_modulus": 16, "length": 10}


def test_recognizer_accepts_struct_ssa_loads():
    spec = recognize_string_decrypt_function(
        decrypt_callee(b"libUE4.so\x00", key_modulus=16, load_op="MLIL_LOAD_STRUCT_SSA")
    )

    assert spec == {"key_modulus": 16, "length": 10}


def test_decoder_recovers_observed_strings():
    bv = FakeBv()
    samples = (
        (b"libUE4.so\x00", b"0123456789abcdef"),
        (b"libGLESv2.so\x00", b"0123456789abcdefghijklm"),
        (b"glDrawElements\x00", b"ABCDEFGHIJKLMNOPQRSTUVW"),
        (b"libtersafe.so\x00", b"ponytail-decode!!"),
    )
    for text, key in samples:
        bv.memory[0x7000] = encoded_blob(text, key)
        spec = {"key_modulus": len(key), "length": len(text)}

        assert decode_string_blob(bv, 0x7000, spec) == text


def test_annotates_current_function_direct_decrypt_calls_and_preserves_comments():
    bv = FakeBv()
    callee = decrypt_callee("glDrawElements")
    bv.functions[callee.start] = callee
    bv.session_data["dispatchthis_mlil_stable"][callee.start] = True
    bv.memory[0x7000] = encoded_blob("glDrawElements")
    caller = FakeFunc(0x1000)
    caller.mlil = FakeMlil([call(const(callee.start), [const(0x6000), const(0x7000)])], caller)
    caller.comments[0x5000] = "manual note\n[decrypt] stale, src=0x1 dst=0x2"

    facts = plan_string_decrypt_calls(
        bv,
        caller,
        caller.mlil,
        bv.session_data["dispatchthis_mlil_stable"],
    )

    assert facts == [{
        "call_addr": 0x5000,
        "src_addr": 0x7000,
        "dst_addr": 0x6000,
        "plaintext": b"glDrawElements",
    }]
    assert apply_decrypted_string_comments(caller, facts) == 1
    assert caller.comments[0x5000] == (
        "manual note\n[decrypt] glDrawElements, src=0x7000 dst=0x6000"
    )
    assert apply_decrypted_string_comments(caller, facts) == 0
    assert annotate_decrypted_string_calls(bv, caller, caller.mlil) == 0


def test_decrypt_comment_appends_replaces_in_place_and_deduplicates():
    func = FakeFunc()
    line = "[decrypt] new, src=0x7 dst=0x6"

    func.comments[0x5000] = "manual note"
    assert _set_decrypt_comment(func, 0x5000, line) is True
    assert func.comments[0x5000] == "manual note\n[decrypt] new, src=0x7 dst=0x6"

    func.comments[0x5000] = (
        "before\n"
        "[decrypt] stale, src=0x1 dst=0x2\n"
        "after\n"
        "[decrypt] duplicate, src=0x3 dst=0x4"
    )
    assert _set_decrypt_comment(func, 0x5000, line) is True
    assert func.comments[0x5000] == "before\n[decrypt] new, src=0x7 dst=0x6\nafter"
    assert _set_decrypt_comment(func, 0x5000, line) is False


def test_decrypt_comment_escapes_single_line_unsafe_bytes():
    expected = (
        "A"
        + "\\0"
        + "\\n"
        + "\\r"
        + "\\t"
        + '\\"'
        + "\\\\"
        + "\\x01"
        + "\\x7f"
        + "\\x80"
        + "Z"
    )

    assert _escaped(b'A\x00\n\r\t"\\\x01\x7f\x80Z') == expected


def test_skips_indirect_non_constant_and_non_stable_calls():
    bv = FakeBv()
    callee = decrypt_callee("libtersafe.so")
    unrecognized = FakeFunc(0x3000, FakeMlil([]))
    bv.functions[callee.start] = callee
    bv.functions[unrecognized.start] = unrecognized
    bv.session_data["dispatchthis_mlil_stable"][unrecognized.start] = True
    bv.memory[0x7000] = encoded_blob("libtersafe.so")
    caller = FakeFunc(0x1000)
    caller.mlil = FakeMlil(
        [
            call(var("dynamic_target"), [const(0x6000), const(0x7000)], address=0x5000),
            call(const(callee.start), [const(0x6000), const(0x7000)], address=0x5010),
            call(const(callee.start), [var("dst"), const(0x7000)], address=0x5020),
            call(const(callee.start), [const(0x6000), var("src")], address=0x5030),
            call(const(callee.start), [const(0x6000)], address=0x5040),
            call(const(unrecognized.start), [const(0x6000), const(0x7000)], address=0x5050),
        ],
        caller,
    )

    logs = []
    old_log_debug = string_decrypt.log_debug
    string_decrypt.log_debug = logs.append
    try:
        assert annotate_decrypted_string_calls(bv, caller, caller.mlil) == 0
    finally:
        string_decrypt.log_debug = old_log_debug
    assert caller.comments == {}
    assert any("skipped fewer than two arguments" in item for item in logs)
    assert any("skipped unrecognized callee" in item for item in logs)


def test_rejects_similar_non_matching_callee_without_done_flag():
    callee = decrypt_callee("libUE4.so")
    callee.mlil.instructions = callee.mlil.instructions[:-1]

    assert recognize_string_decrypt_function(callee) is None


if __name__ == "__main__":
    test_recognizer_matches_sample_family_decrypt_shape()
    test_recognizer_accepts_struct_ssa_loads()
    test_decoder_recovers_observed_strings()
    test_annotates_current_function_direct_decrypt_calls_and_preserves_comments()
    test_decrypt_comment_appends_replaces_in_place_and_deduplicates()
    test_decrypt_comment_escapes_single_line_unsafe_bytes()
    test_skips_indirect_non_constant_and_non_stable_calls()
    test_rejects_similar_non_matching_callee_without_done_flag()
