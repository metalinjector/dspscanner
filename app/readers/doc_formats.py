"""
Определение реального формата файла с расширением .doc и набор
специализированных парсеров под каждый формат.

Файлы .doc "в дикой природе" встречаются в нескольких видах (аналог
`Get-DocRealFormat` из ps1):
    * OLE2 / Compound File Binary  — "настоящий" бинарный формат Word 97-2003;
    * ZIP (PK..)                    — на самом деле DOCX, у которого просто
                                       заменили/сохранили под старым расширением;
    * RTF ("{\\rtf")                 — Rich Text Format;
    * HTML ("<html", "<!doctype")   — веб-страница, сохранённая как .doc;
    * XML ("<?xml", Word 2003 XML)  — WordprocessingML (Word 2003 XML);
    * UNKNOWN                       — не распознано, пробуем как обычный текст.
"""
from __future__ import annotations

import io
import re
from typing import Optional
from defusedxml import ElementTree as ET

OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_SIGNATURE = b"PK\x03\x04"


def detect_format(data: bytes) -> str:
    if len(data) < 8:
        return "UNKNOWN"

    head = data[:8]
    if head == OLE2_SIGNATURE:
        return "OLE2"
    if head[:4] == ZIP_SIGNATURE:
        return "ZIP"

    sniff = data[:512].lstrip()
    low = sniff[:200].lower()
    if sniff.startswith(b"{\\rtf"):
        return "RTF"
    if b"<html" in low or b"<!doctype html" in low:
        return "HTML"
    if sniff.startswith(b"<?xml") or b"<w:wordDocument" in data[:2000]:
        return "XML"
    return "UNKNOWN"


# --------------------------------------------------------------------------- #
# RTF
# --------------------------------------------------------------------------- #

def read_rtf(data: bytes) -> Optional[str]:
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError:
        return _read_rtf_fallback(data)

    for enc in ("cp1251", "utf-8", "latin-1"):
        try:
            raw = data.decode(enc, errors="ignore")
            text = rtf_to_text(raw)
            text = text.strip()
            if len(text) >= 5:
                return text
        except Exception:
            continue
    return None


def _read_rtf_fallback(data: bytes) -> Optional[str]:
    try:
        raw = data.decode("latin-1", errors="ignore")
        raw = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)
        raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", raw)
        raw = re.sub(r"[{}]", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw if len(raw) >= 5 else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def read_html(data: bytes) -> Optional[str]:
    encoding = "utf-8"
    match = re.search(rb'charset=["\']?([\w-]+)', data[:2000], re.IGNORECASE)
    if match:
        try:
            encoding = match.group(1).decode("ascii")
        except Exception:
            encoding = "utf-8"

    try:
        html_text = data.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html_text = data.decode("cp1251", errors="replace")

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html_text)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) >= 5 else None


# --------------------------------------------------------------------------- #
# Word 2003 XML (WordprocessingML)
# --------------------------------------------------------------------------- #

def read_word_xml(data: bytes) -> Optional[str]:
    try:
        text_bytes = data
        root = ET.fromstring(text_bytes)
        parts = []
        for elem in root.iter():
            if elem.tag.endswith("}t") or elem.tag == "t":
                if elem.text:
                    parts.append(elem.text)
            elif elem.tag.endswith("}p") or elem.tag == "p":
                parts.append("\n")
        result = "".join(parts)
        result = re.sub(r"[ \t]+", " ", result)
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        if len(result) >= 5:
            return result
    except ET.ParseError:
        pass
    except Exception:
        pass

    # Резерв: снимаем теги регуляркой
    try:
        raw = data.decode("utf-8", errors="replace")
        cleaned = re.sub(r"<[^>]+>", " ", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned if len(cleaned) >= 5 else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Обычный текст (последний рубеж)
# --------------------------------------------------------------------------- #

def read_as_plain_text(data: bytes) -> Optional[str]:
    try:
        text = data.decode("utf-8")
        if re.search(r"[\x00-\x08\x0e-\x1f]", text):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "control chars")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")

    text = re.sub(r"[^\w\s\u0400-\u04FF.,;:!?()«»\"'-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) >= 10 else None


# --------------------------------------------------------------------------- #
# OLE2 эвристический разбор (аналог Read-DocOLE2 из ps1, но точнее:
# сначала пытаемся найти именно поток WordDocument через olefile,
# а не сканировать весь файл целиком, что резко снижает "шум" из
# служебных потоков (SummaryInformation и т.п.))
# --------------------------------------------------------------------------- #

def read_ole2_heuristic(data: bytes) -> Optional[str]:
    stream = _extract_word_document_stream(data) or data
    unicode_text = _scan_unicode_words(stream)
    if unicode_text and len(unicode_text) >= 50:
        return unicode_text

    ansi_text = _scan_ansi_words(stream)
    if ansi_text and len(ansi_text) > len(unicode_text or "") * 1.5:
        return ansi_text

    return unicode_text or ansi_text


def _extract_word_document_stream(data: bytes) -> Optional[bytes]:
    try:
        import olefile
    except ImportError:
        return None
    try:
        if not olefile.isOleFile(data=data):
            return None
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            for candidate in ("WordDocument", "1Table", "0Table"):
                if ole.exists(candidate):
                    try:
                        return ole.openstream(candidate).read()
                    except Exception:
                        continue
    except Exception:
        return None
    return None


def _scan_unicode_words(data: bytes) -> Optional[str]:
    """Извлекает ASCII/кириллические слова из предполагаемого UTF-16LE-потока.

    Декодируем буфер как UTF-16-LE (errors='ignore') и извлекаем слова
    регулярным выражением. Семантика сохранена: только ASCII-буквы/цифры
    (U+0041..U+005A, U+0061..U+007A, U+0030..U+0039) и кириллица
    (U+0400..U+04FF) участвуют в словах; любой разделитель завершает
    слово, а слишком короткие фрагменты (<3 символов) отбрасываются.
    """
    decoded = data.decode("utf-16-le", errors="ignore")
    if not decoded:
        return None

    # Оставляем только ASCII-буквы/цифры и кириллицу; всё прочее — разделитель.
    cleaned = re.sub(r"[^A-Za-z0-9\u0400-\u04FF]", " ", decoded)
    # Отбрасываем «слова» короче 3 символов, как в оригинальном алгоритме.
    words = [w for w in cleaned.split() if len(w) >= 3]
    result = " ".join(words).strip()
    return result if len(result) > 10 else None


def _scan_ansi_words(data: bytes) -> Optional[str]:
    try:
        text = data.decode("cp1251", errors="ignore")
    except Exception:
        return None
    text = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 10 else None
