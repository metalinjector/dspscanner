"""Regression corpus for strict Russian OCR and non-OCR document paths."""
from __future__ import annotations

from email.message import EmailMessage
import struct
import subprocess
import zipfile

import pytest

from app.config import DocReadMethod, ScanSettings
from app.readers import extract_text
from app.readers.doc_formats import read_html
from app.readers.doc_reader import DocReader
from app.readers.docx_reader import DocxReader
from app.readers.ole_doc import EncryptedDocError, parse_piece_table
from app.readers.txt_reader import TxtReader
from app.readers import pdf_reader as pdf
from app.scanning import search_engine as search
from tools.text_corpus import binary_doc, docx, word_streams, WORD_NS


@pytest.mark.parametrize("available,strict,expected", [
    ("eng", True, None), ("eng", False, "eng"),
    ("rus\neng", True, "rus"), ("rus\neng", False, "rus+eng"),
    ("rus", True, "rus"), ("", True, None),
])
def test_strict_russian_language_never_falls_back(tmp_path, monkeypatch, available, strict, expected):
    monkeypatch.setattr(pdf, "_ocr_lang_done", False)
    monkeypatch.setattr(pdf, "_run_tesseract", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, available.encode(), b""))
    assert pdf._resolve_ocr_lang(str(tmp_path / "tesseract"), strict) == expected


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                                      "utf-32", "utf-32-le", "utf-32-be", "cp1251", "koi8-r", "cp866"])
def test_txt_encoding_matrix(tmp_path, encoding):
    text = "Договор РФ и счёт. English contract.\nВторая строка."
    path = tmp_path / "source.txt"
    path.write_bytes(text.encode(encoding))
    outcome = TxtReader().extract_text(path)
    assert not outcome.error
    assert outcome.text == text
    if encoding in ("cp1251", "koi8-r", "cp866"):
        assert outcome.warning  # guessed encoding is not a certainty


@pytest.mark.parametrize("encoding", ["cp1251", "koi8-r", "cp866"])
def test_short_russian_legacy_txt(tmp_path, encoding):
    path = tmp_path / "short.txt"
    path.write_bytes("Договор".encode(encoding))
    assert TxtReader().extract_text(path).text == "Договор"


def test_utf8_formfeed_does_not_need_charset_detector(tmp_path, monkeypatch):
    from app.readers import txt_reader
    monkeypatch.setattr(txt_reader, "_cn_from_bytes", lambda *a, **k: pytest.fail("unnecessary detector"))
    path = tmp_path / "source.txt"
    path.write_bytes("Договор\fEnglish contract\tтекст".encode())
    assert TxtReader().extract_text(path).method_used == "utf-8"


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("extension", ["txt", "docx", "doc"])
def test_russian_ocr_setting_does_not_filter_regular_documents(tmp_path, monkeypatch, strict, extension):
    monkeypatch.setenv("DSP_SCANNER_RUSSIAN_ONLY", str(strict))
    text = "Договор РФ. English contract."
    path = tmp_path / f"sample.{extension}"
    if extension == "txt":
        path.write_text(text, encoding="utf-8")
    elif extension == "docx":
        docx(path, [text])
    else:
        binary_doc(path, text)
    outcome = extract_text(path, "." + extension, ScanSettings())
    assert outcome.text == text
    found = search.find_matches(outcome.text, ["договор", "РФ", "contract"], whole_word=True)
    assert [m.word for m in found] == ["договор", "РФ", "contract"]


def mhtml(text):
    message = EmailMessage()
    message.set_content(text, subtype="html", charset="utf-8")
    return message.as_string()


def test_all_altchunks_entities_inline_runs_and_repeated_occurrences(tmp_path):
    path = docx(tmp_path / "many.docx", ["Договор"], {
        "word/afchunk1.mht": mhtml("<p><b>Дого</b>вор &amp; счёт</p>"),
        "word/afchunk2.mht": mhtml("<p>Договор</p>"),
    })
    outcome = DocxReader().extract_text(path)
    assert outcome.text.count("Договор") == 3
    assert "& счёт" in outcome.text
    assert len(search.find_matches(outcome.text, ["договор"])) == 3


def test_altchunk_only_does_not_open_python_docx_fallback(tmp_path, monkeypatch):
    path = docx(tmp_path / "only.docx", [], {"word/import.mht": mhtml("<p>ДСП</p>")})
    monkeypatch.setattr(DocxReader, "_read_with_python_docx", lambda *a: pytest.fail("unnecessary fallback"))
    assert DocxReader().extract_text(path).text == "ДСП"


def test_mime_alternatives_are_not_two_occurrences(tmp_path):
    message = EmailMessage()
    message.set_content("Договор")
    message.add_alternative("<p>Договор</p>", subtype="html")
    path = docx(tmp_path / "alternative.docx", [], {"word/import.mht": message.as_string()})
    assert DocxReader().extract_text(path).text == "Договор"


@pytest.mark.parametrize("namespace", [WORD_NS, "http://purl.oclc.org/ooxml/wordprocessingml/main"])
def test_docx_runs_nested_textbox_headers_and_hyphens(tmp_path, namespace):
    path = docx(tmp_path / "runs.docx", [])
    xml = f'''<w:document xmlns:w="{namespace}"><w:body>
    <w:p><w:r><w:t>Дого</w:t></w:r><w:r><w:t>вор</w:t></w:r>
      <w:p><w:r><w:t>Текстовое поле</w:t></w:r></w:p><w:r><w:t>После поля</w:t></w:r></w:p>
    <w:p><w:r><w:t>ZX</w:t><w:noBreakHyphen/><w:t>2048</w:t></w:r></w:p>
    </w:body></w:document>'''
    # Rebuild, do not create duplicate central directory names.
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/header1.xml", f'<w:hdr xmlns:w="{namespace}"><w:p><w:r><w:t>Колонтитул</w:t></w:r></w:p></w:hdr>')
        archive.writestr("word/comments.xml", "<broken>")
    outcome = DocxReader().extract_text(path)
    for term in ("Договор", "Текстовое поле", "После поля", "ZX-2048", "Колонтитул"):
        assert term in outcome.text
    assert "полеПосле" not in outcome.text
    assert "comments.xml" in outcome.warning


def test_html_doc_keeps_inline_words_and_block_boundaries():
    assert read_html('<p>Дого<b>вор</b></p><p>РФ &amp; счёт</p>'.encode()) == "Договор\n\nРФ & счёт"


def test_piece_table_preserves_short_words_numbers_and_punctuation():
    text = "РФ и я: ДСП, ZX-2048.\rEnglish contract."
    word, table = word_streams(text)
    assert parse_piece_table(word, table) == text.replace("\r", "\n")


def test_piece_table_field_results_not_instructions():
    word, table = word_streams('До \x13 HYPERLINK "hidden" \x14Договор\x15 после')
    assert parse_piece_table(word, table) == "До Договор после"


@pytest.mark.parametrize("offset,value", [(418, 9999999), (422, 9999999), (152, 65535)])
def test_piece_table_rejects_corrupt_fib(offset, value):
    word, table = word_streams("Договор")
    broken = bytearray(word)
    struct.pack_into("<H" if offset == 152 else "<I", broken, offset, value)
    with pytest.raises(ValueError):
        parse_piece_table(bytes(broken), table)


def test_piece_table_rejects_outside_piece_and_expansion():
    word, table = word_streams("Договор")
    for offset, value in [(15, 9999999), (9, 100_000_000)]:
        broken = bytearray(table)
        struct.pack_into("<I", broken, offset, value)
        with pytest.raises(ValueError):
            parse_piece_table(word, bytes(broken))


def test_encrypted_doc_not_scanned_as_binary_garbage():
    word, table = word_streams("Договор")
    broken = bytearray(word)
    struct.pack_into("<H", broken, 10, 0x0304)
    with pytest.raises(EncryptedDocError):
        parse_piece_table(bytes(broken), table)


def test_native_doc_auto_avoids_heuristic_and_external_programs(tmp_path, monkeypatch):
    path = binary_doc(tmp_path / "native.doc", "Договор РФ и contract.")
    monkeypatch.setattr(DocReader, "_heuristic_fallback", lambda *a, **k: pytest.fail("heuristic"))
    monkeypatch.setattr(DocReader, "_via_libreoffice", lambda *a, **k: pytest.fail("external"))
    outcome = DocReader().extract_text(path)
    assert outcome.method_used == "ole2-piece-table"
    assert outcome.text == "Договор РФ и contract."


def test_explicit_heuristic_retains_warning(tmp_path):
    path = binary_doc(tmp_path / "native.doc", "Договор РФ и contract.")
    outcome = DocReader().extract_text(path, DocReadMethod.HEURISTIC)
    assert outcome.warning and "эвристически" in outcome.warning


@pytest.mark.parametrize("capital_dotted_i", ["", "İ"])
def test_aho_and_regex_same_semantics_on_unicode(monkeypatch, capital_dotted_i):
    words = ["договор", "РФ", "счет", "I", "C++", "постав*", "новый договор"] + [f"нет{i}" for i in range(35)]
    text = "Договор РФ. счёт СЧЕТ. I ı i. C++ поставщик новый\nдоговор." + capital_dotted_i
    def result(threshold):
        monkeypatch.setattr(search, "_AHO_MIN_TERMS", threshold)
        return [(m.word, m.position, m.matched_text) for m in search.find_matches(text, words, whole_word=True)]
    assert result(1) == result(10000)


def test_piece_table_orders_mixed_compressed_and_unicode_pieces():
    word, _ = word_streams("")
    word = bytearray(word)
    first, second = "РФ и ", "contract"
    word[1300:1300 + len(first.encode('utf-16-le'))] = first.encode('utf-16-le')
    word[1024:1024 + len(second)] = second.encode('cp1252')
    positions = struct.pack('<III', 0, len(first), len(first) + len(second))
    pcds = struct.pack('<HIHHIH', 0, 1300, 0, 0, 0x40000000 | 2048, 0)
    plc = positions + pcds
    table = b'\x01\x01\x00\x00' + b'\x02' + struct.pack('<I', len(plc)) + plc
    struct.pack_into('<II', word, 418, 0, len(table))
    struct.pack_into('<I', word, 76, len(first) + len(second))
    assert parse_piece_table(bytes(word), table) == "РФ и contract"


def test_doc_control_nonbreaking_hyphen_is_not_dropped():
    word, table = word_streams("Номер ZX\x1e2048")
    assert parse_piece_table(word, table) == "Номер ZX-2048"


@pytest.mark.parametrize('text,encoding', [
    ('Für Prüfung über Größe und äußere Änderungen.', 'cp1252'),
    ('Zażółć gęślą jaźń. To jest dokument.', 'cp1250'),
    ('合同协议 中文文本', 'utf-8'),
])
def test_russian_legacy_hint_does_not_replace_these_other_encodings(tmp_path, text, encoding):
    path = tmp_path / 'international.txt'
    path.write_bytes(text.encode(encoding))
    assert TxtReader().extract_text(path).text == text


def test_bad_import_does_not_hide_later_import(tmp_path):
    path = docx(tmp_path / 'partial.docx', ['Обычный текст'], {
        'word/bad.mht': 'Content-Type: application/octet-stream\r\n\r\nunsupported',
        'word/good.mht': mhtml('<p>Договор</p>'),
    })
    result = DocxReader().extract_text(path)
    assert 'Договор' in result.text
    assert 'bad.mht' in result.warning


@pytest.mark.parametrize('kind', ['rtf', 'html', 'xml', 'zip'])
def test_disguised_doc_routes_to_format_parser(tmp_path, kind):
    path = tmp_path / f'{kind}.doc'
    if kind == 'zip':
        docx(path, ['Договор РФ contract'])
    elif kind == 'rtf':
        path.write_bytes(b'{\\rtf1\\ansi\\ansicpg1251 ' + 'Договор РФ contract'.encode('cp1251') + b'}')
    elif kind == 'html':
        path.write_bytes('<html><body><p>Договор РФ contract</p></body></html>'.encode())
    else:
        path.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>' +
                         '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml"><w:p><w:r><w:t>Договор РФ contract</w:t></w:r></w:p></w:wordDocument>'.encode())
    result = DocReader().extract_text(path)
    assert not result.error
    assert result.text == 'Договор РФ contract'


def test_scanner_doc_docx_txt_end_to_end_with_spawn(tmp_path, monkeypatch):
    from app.scanning.scanner import DocumentScanner
    monkeypatch.setenv('DSP_SCANNER_RUSSIAN_ONLY', 'true')
    monkeypatch.setenv('DSP_SCANNER_LIBREOFFICE_PATH', str(tmp_path / 'missing-soffice'))
    text = 'Договор РФ contract.'
    binary_doc(tmp_path / 'native.doc', text)
    docx(tmp_path / 'modern.docx', [text])
    (tmp_path / 'plain.txt').write_bytes(text.encode('cp1251'))
    settings = ScanSettings(paths=[str(tmp_path)], words=['договор', 'РФ', 'contract'],
                            file_types={'.doc', '.docx', '.txt'}, max_workers=2,
                            whole_word=True, use_ocr_for_pdf=False)
    report = DocumentScanner().run(settings)
    assert report.stats.processed == 3
    assert report.stats.errors == 0
    assert len(report.results) == 9
    assert {r.file_type for r in report.results} == {'doc', 'docx', 'txt'}


@pytest.mark.parametrize('kind', ['rtf', 'rtf_utf8_bom', 'xml', 'html'])
def test_short_text_in_declared_doc_formats_is_not_discarded(tmp_path, kind):
    path = tmp_path / 'short.doc'
    if kind == 'rtf':
        data = b'{\\rtf1\\ansi\\ansicpg1251 ' + 'ДСП'.encode('cp1251') + b'}'
    elif kind == 'rtf_utf8_bom':
        data = b'\xef\xbb\xbf{\\rtf1\\ansi\\ansicpg65001 ' + 'ДСП'.encode() + b'}'
    elif kind == 'xml':
        data = '<?xml version="1.0" encoding="UTF-8"?><root><p><t>ДСП</t></p></root>'.encode()
    else:
        data = '<html><body>ДСП</body></html>'.encode()
    path.write_bytes(data)
    assert DocReader().extract_text(path).text == 'ДСП'


def test_word2003_xml_separates_tabs_and_joins_formatting_runs():
    from app.readers.doc_formats import read_word_xml
    text = '<root><p><t>Дого</t><t>вор</t><tab/><t>РФ</t></p><p><t>contract</t></p></root>'
    assert ' '.join(read_word_xml(text.encode()).split()) == 'Договор РФ contract'


@pytest.mark.parametrize('text', ['РФ', 'Δοκιμή κείμενο'])
@pytest.mark.parametrize('encoding', ['utf-16-le', 'utf-16-be'])
def test_bomless_unicode_not_mistaken_for_utf8_control_bytes(tmp_path, text, encoding):
    path = tmp_path / 'unicode.txt'
    path.write_bytes(text.encode(encoding))
    assert TxtReader().extract_text(path).text == text
