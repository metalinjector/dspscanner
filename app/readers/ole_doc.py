"""Bounded Word 97–2007 text-piece reader (MS-DOC Fib/Clx/PlcPcd).

Reads logical text pieces rather than scanning binary metadata for strings.
Not a Word renderer: no OCR, embedded objects, layout or revision visibility.
Unsupported/corrupt formats return None so the caller can use explicit fallbacks.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

_SUPPORTED_FIB = {0x00C1, 0x00D9, 0x0101, 0x010C, 0x0112}
_MAX_STREAM_BYTES = 128 * 1024 * 1024
_MAX_PIECES = 1_000_000
_MAX_TEXT_CHARS = 32 * 1024 * 1024
_CONTROL_TRANSLATION = {i: None for i in range(32)}
_CONTROL_TRANSLATION.update({7: "\t", 9: "\t", 10: "\n", 11: "\n", 12: "\n", 13: "\n", 30: "-"})


class EncryptedDocError(ValueError):
    pass


def _uint(data: bytes, offset: int, size: int = 4) -> int:
    if offset < 0 or offset + size > len(data):
        raise ValueError("Truncated Word structure")
    return struct.unpack_from("<H" if size == 2 else "<I", data, offset)[0]


def _clean_story_text(text: str) -> str:
    if not any(c in text for c in "\x13\x14\x15"):
        return re.sub(r"\n{3,}", "\n\n", text.translate(_CONTROL_TRANSLATION)).strip()
    # Field instructions are not visible text; nested fields keep their result.
    visible = []
    fields: list[bool] = []
    hidden_depth = 0
    for char in text:
        if char == "\x13":
            fields.append(False)
            hidden_depth += 1
        elif char == "\x14" and fields:
            if not fields[-1]:
                hidden_depth -= 1
            fields[-1] = True
        elif char == "\x15" and fields:
            if not fields.pop():
                hidden_depth -= 1
        elif hidden_depth == 0:
            visible.append(char)
    return re.sub(r"\n{3,}", "\n\n", "".join(visible).translate(_CONTROL_TRANSLATION)).strip()



def parse_piece_table(word: bytes, table: bytes) -> str:
    """Raises ValueError on invalid offsets/counts; never scans a partial CLX."""
    if _uint(word, 0, 2) != 0xA5EC or _uint(word, 2, 2) not in _SUPPORTED_FIB:
        raise ValueError("Unsupported Word version")
    flags = _uint(word, 10, 2)
    if flags & (0x0100 | 0x8000):
        raise EncryptedDocError("Зашифрованный DOC: требуется пароль")
    # Skip FibRgW and FibRgLw using their declared sizes (not a hardcoded fcClx).
    csw = _uint(word, 32, 2)
    lw_offset = 34 + csw * 2
    cslw = _uint(word, lw_offset, 2)
    pairs_offset = lw_offset + 2 + cslw * 4
    count = _uint(word, pairs_offset, 2)
    if count <= 33 or pairs_offset + 2 + count * 8 > len(word):
        raise ValueError("Missing FibRgFcLcb97.fcClx")
    clx_offset = _uint(word, pairs_offset + 2 + 33 * 8)
    clx_size = _uint(word, pairs_offset + 6 + 33 * 8)
    if not clx_size or clx_offset + clx_size > len(table):
        raise ValueError("Invalid CLX range")
    clx = table[clx_offset:clx_offset + clx_size]
    cursor = 0
    while cursor < len(clx) and clx[cursor] == 1:  # optional Prc records
        size = _uint(clx, cursor + 1, 2)
        cursor += 3 + size
    if cursor >= len(clx) or clx[cursor] != 2:
        raise ValueError("Missing Pcdt")
    size = _uint(clx, cursor + 1)
    start = cursor + 5
    if size < 4 or (size - 4) % 12 or start + size != len(clx):
        raise ValueError("Invalid PlcPcd size")
    plc = clx[start:]
    count = (size - 4) // 12
    if count > _MAX_PIECES or _uint(plc, 0) != 0:
        raise ValueError("Invalid CP count/origin")
    pieces = []
    previous_end = 0
    for index in range(count):
        cp_start = _uint(plc, index * 4)
        cp_end = _uint(plc, (index + 1) * 4)
        if cp_start != previous_end or cp_end < cp_start:
            raise ValueError("Non-monotonic CP positions")
        if cp_end > _MAX_TEXT_CHARS:
            raise ValueError("Text expansion limit exceeded")
        previous_end = cp_end
        length = cp_end - cp_start
        fc = _uint(plc, (count + 1) * 4 + index * 8 + 2)
        if fc & 0x80000000:
            raise ValueError("Reserved FcCompressed bit set")
        compressed = bool(fc & 0x40000000)
        offset = fc & 0x3FFFFFFF
        if compressed:
            if offset % 2:
                raise ValueError("Unaligned compressed FC")
            offset //= 2
        end = offset + length * (1 if compressed else 2)
        if offset < pairs_offset + 2 + _uint(word, pairs_offset, 2) * 8:
            raise ValueError("Text piece points inside FIB")
        if end > len(word) or end - offset > _MAX_STREAM_BYTES:
            raise ValueError("Text piece outside WordDocument")
        # Compressed Word97 Unicode uses Windows-1252, not the host code page.
        pieces.append(word[offset:end].decode("cp1252" if compressed else "utf-16-le"))
    return _clean_story_text("".join(pieces))


def read_ole2_text(path: Path) -> str | None:
    try:
        import olefile
    except ImportError:
        return None
    try:
        with olefile.OleFileIO(str(path)) as ole:
            if ole.exists("EncryptedPackage"):
                raise EncryptedDocError("Зашифрованный DOC: требуется пароль")
            if not ole.exists("WordDocument") or ole.get_size("WordDocument") > _MAX_STREAM_BYTES:
                return None
            word = ole.openstream("WordDocument").read()
            flags = _uint(word, 10, 2)
            if flags & (0x0100 | 0x8000):
                raise EncryptedDocError("Зашифрованный DOC: требуется пароль")
            name = "1Table" if flags & 0x0200 else "0Table"
            if not ole.exists(name) or ole.get_size(name) > _MAX_STREAM_BYTES:
                return None
            return parse_piece_table(word, ole.openstream(name).read())
    except EncryptedDocError:
        raise
    except (OSError, ValueError, struct.error, UnicodeError):
        return None
