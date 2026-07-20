from conftest import load_plugin_module


semantics = load_plugin_module("plugins.DispatchThis.semantics")
string_decrypt = load_plugin_module("plugins.DispatchThis.passes.medium.decrypt")

apply_decrypted_string_comments = string_decrypt.apply_decrypted_string_comments
_escaped = string_decrypt._escaped
_set_decrypt_comment = string_decrypt._set_decrypt_comment


class FakeFunc:
    def __init__(self):
        self.comments = {}

    def get_comment_at(self, address):
        return self.comments.get(address, "")

    def set_comment_at(self, address, text):
        self.comments[address] = text


def test_decrypt_comment_preserves_manual_lines_and_is_idempotent():
    func = FakeFunc()
    func.comments[0x5000] = "manual note\n[decrypt] manually written text"
    fact = semantics.StringRecoveryFact(0x5000, 0x7000, 0x6000, b"glDrawElements")

    assert apply_decrypted_string_comments(func, (fact,)) == 1
    assert func.comments[0x5000] == (
        "manual note\n"
        "[decrypt] manually written text\n"
        "[decrypt] glDrawElements, src=0x7000 dst=0x6000"
    )
    assert apply_decrypted_string_comments(func, (fact,)) == 0


def test_decrypt_comment_preserves_manual_text_and_existing_line_endings():
    func = FakeFunc()
    func.comments[0x5000] = "manual one\r\nmanual tail\r\n"
    fact = semantics.StringRecoveryFact(0x5000, 0x7000, 0x6000, b"plain")

    assert apply_decrypted_string_comments(func, (fact,)) == 1
    assert func.comments[0x5000] == (
        "manual one\r\n"
        "manual tail\r\n"
        "[decrypt] plain, src=0x7000 dst=0x6000"
    )


def test_decrypt_comment_updates_and_deduplicates_dispatchthis_lines():
    func = FakeFunc()
    func.comments[0x5000] = (
        "before\n"
        "[DispatchThis decrypt] stale, src=0x1 dst=0x2\n"
        "after\n"
        "[DispatchThis decrypt] duplicate, src=0x3 dst=0x4"
    )
    changed = semantics.StringRecoveryFact(0x5000, 0x7001, 0x6001, b"new")

    assert apply_decrypted_string_comments(func, (changed,)) == 1
    assert func.comments[0x5000] == (
        "before\n"
        "[decrypt] new, src=0x7001 dst=0x6001\n"
        "after\n"
    )
    assert _set_decrypt_comment(
        func,
        0x5000,
        "[decrypt] new, src=0x7001 dst=0x6001",
    ) is False


def test_decrypt_comment_escapes_binary_plaintext_and_keeps_printable_utf8():
    assert _escaped(b'A\x00\n\r\t"\\\x01\x7f\x80Z') == (
        "A\\0\\n\\r\\t\\\"\\\\\\x01\\x7f\\x80Z"
    )
    text = "是否使用上次启动配置(Y/N):\n👇🏻"
    assert _escaped(text.encode("utf-8") + b"\x00") == text.replace("\n", "\\n") + "\\0"
