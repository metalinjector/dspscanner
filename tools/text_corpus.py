"""Deterministic, self-authored DOC/DOCX/TXT fixtures; no office installation."""
from __future__ import annotations

import struct
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def word_streams(text: str) -> tuple[bytes, bytes]:
    """One Unicode piece in a Word97 Fib/CLX; useful for bounds tests too."""
    payload = text.encode("utf-16-le")
    count = len(payload) // 2
    word = bytearray(max(4096, 1024 + len(payload)))
    struct.pack_into("<HH", word, 0, 0xA5EC, 0x00C1)
    struct.pack_into("<H", word, 10, 0x0204)
    struct.pack_into("<II", word, 24, 1024, 1024 + len(payload))
    struct.pack_into("<H", word, 32, 14)
    struct.pack_into("<H", word, 62, 22)
    struct.pack_into("<I", word, 76, count)
    struct.pack_into("<H", word, 152, 93)
    plc = struct.pack("<IIHIH", 0, count, 0, 1024, 0)
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 418, 0, len(clx))
    word[1024:1024 + len(payload)] = payload
    return bytes(word), clx.ljust(4096, b"\x00")


def binary_doc(path: Path, text: str) -> Path:
    """Minimal CFB v3 with two regular streams; tests are not Word round-trips."""
    word, table = word_streams(text)
    streams = [word, table]
    counts = [(len(data) + 511) // 512 for data in streams]
    data_sectors = 1 + sum(counts)  # directory + streams
    fat_count = 1
    while fat_count * 128 < data_sectors + fat_count:
        fat_count += 1
    if fat_count > 109:
        raise ValueError("Fixture too large for header DIFAT")
    free, end, fat_sector = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    header = bytearray(512)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, fat_count, 0, 0, 4096, end, 0, end, 0)
    difat = list(range(data_sectors, data_sectors + fat_count))
    struct.pack_into("<109I", header, 76, *(difat + [free] * (109 - fat_count)))

    def entry(name, kind, start, size, child=free, left=free, color=1):
        value = bytearray(128)
        encoded = (name + "\x00").encode("utf-16-le")
        value[:len(encoded)] = encoded
        struct.pack_into("<HBBIII", value, 64, len(encoded), kind, color, left, free, child)
        struct.pack_into("<IQ", value, 116, start, size)
        return bytes(value)

    directory = (entry("Root Entry", 5, end, 0, child=1)
                 + entry("WordDocument", 2, 1, len(word), left=2)
                 + entry("1Table", 2, 1 + counts[0], len(table), color=0)
                 + bytes(128))
    fat = [free] * (fat_count * 128)
    fat[0] = end
    cursor = 1
    for count in counts:
        for index in range(cursor, cursor + count):
            fat[index] = index + 1 if index < cursor + count - 1 else end
        cursor += count
    for index in difat:
        fat[index] = fat_sector
    path.write_bytes(bytes(header) + directory + b"".join(data.ljust(count * 512, b"\x00")
                     for data, count in zip(streams, counts)) + struct.pack(f"<{len(fat)}I", *fat))
    return path


def docx(path: Path, lines: list[str], extra_parts: dict[str, str] | None = None) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in lines)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>')
        for name, content in (extra_parts or {}).items():
            archive.writestr(name, content)
    return path
