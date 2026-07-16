"""Core backend for provider-proven string recovery facts."""

from collections.abc import Iterable
from typing import Final

from ...semantics import StringRecoveryFact
from ...utils.log import log_debug, log_info


_COMMENT_PREFIX: Final = "[decrypt] "
_LEGACY_COMMENT_PREFIX: Final = "[DispatchThis decrypt] "
_LINE_ENDINGS: Final = (
    "\r\n",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


def _escaped(data: bytes) -> str:
    """Format plaintext for a single-line DispatchThis decrypt comment."""
    text = data.decode("utf-8", errors="surrogateescape")
    out = []
    for character in text:
        code = ord(character)
        if code == 0:
            out.append("\\0")
        elif code == 9:
            out.append("\\t")
        elif code == 10:
            out.append("\\n")
        elif code == 13:
            out.append("\\r")
        elif character in ('"', "\\"):
            out.append("\\" + character)
        elif 32 <= code <= 126:
            out.append(character)
        elif code < 32 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        elif 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif character.isprintable():
            out.append(character)
        else:
            out.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8"))
    return "".join(out)


def _comment_line(text: str, source_addr: int, destination_addr: int) -> str:
    return f"{_COMMENT_PREFIX}{text}, src={hex(source_addr)} dst={hex(destination_addr)}"


def _split_line_ending(item: str) -> tuple[str, str]:
    for ending in _LINE_ENDINGS:
        if item.endswith(ending):
            return item[:-len(ending)], ending
    return item, ""


def _is_generated_decrypt_line(content: str) -> bool:
    if content.startswith(_LEGACY_COMMENT_PREFIX):
        return True
    return (
        content.startswith(_COMMENT_PREFIX)
        and ", src=0x" in content
        and " dst=0x" in content
    )


def _set_decrypt_comment(func: object, address: int, line: str) -> bool:
    get_comment_at = getattr(func, "get_comment_at", None)
    set_comment_at = getattr(func, "set_comment_at", None)
    if get_comment_at is None or set_comment_at is None:
        log_debug(f"[sdecrypt] {hex(address)}: skipped missing function comment API")
        return False
    old = get_comment_at(address) or ""
    new_lines: list[str] = []
    replaced = False
    for item in old.splitlines(keepends=True):
        content, ending = _split_line_ending(item)
        if _is_generated_decrypt_line(content):
            if not replaced:
                new_lines.append(line + ending)
                replaced = True
            continue
        new_lines.append(item)
    if not replaced:
        if old and not any(old.endswith(ending) for ending in _LINE_ENDINGS):
            new_lines.append("\n")
        new_lines.append(line)
    new = "".join(new_lines)
    if new == old:
        return False
    set_comment_at(address, new)
    return True


def apply_decrypted_string_comments(
    func: object,
    facts: Iterable[StringRecoveryFact],
) -> int:
    """Idempotently apply exact provider facts without touching manual comment lines."""
    changed = 0
    for fact in facts:
        if type(fact) is not StringRecoveryFact:
            continue
        line = _comment_line(_escaped(fact.plaintext), fact.source_addr, fact.destination_addr)
        if _set_decrypt_comment(func, fact.call_addr, line):
            changed += 1
            log_info(f"[sdecrypt] {hex(fact.call_addr)}: {line}")
    return changed
