"""Универсальный читатель legacy .doc с безопасными внешними конвертерами."""
from __future__ import annotations

import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from app.config import DocReadMethod
from app.readers import doc_formats
from app.readers.base import BaseReader, ReaderOutcome
from app.settings_store import load_settings


def _find_libreoffice(settings=None) -> Optional[str]:
    if settings is None:
        settings = load_settings()
    if settings.libreoffice_path and Path(settings.libreoffice_path).is_file():
        return settings.libreoffice_path
    return None


def _release_word_all() -> None:
    """Освобождает оставшиеся COM-ссылки в текущем потоке, если они есть."""
    import gc

    gc.collect()
    try:
        import pythoncom  # type: ignore
        pythoncom.CoUninitialize()
    except Exception:
        return


class DocReader(BaseReader):
    name = "doc"

    @staticmethod
    def external_programs_may_be_available() -> bool:
        """Есть ли смысл запускать второй, внешний проход.

        LibreOffice можно проверить по настроенному исполняемому файлу. На
        Windows MS Word COM определяется только фактической попыткой запуска,
        поэтому считаем его потенциально доступным.
        """
        return bool(_find_libreoffice()) or platform.system() == "Windows"

    def extract_text_external(self, path: Path) -> ReaderOutcome:
        """Извлекает текст только внешними программами, без эвристики.

        Метод предназначен для второго прохода после неудачной быстрой
        эвристики. Важно не вызывать ``_heuristic_fallback`` повторно: иначе
        программа могла бы сообщить об успешном внешнем проходе, фактически
        снова использовав тот же эвристический алгоритм.
        """
        if not self.is_file_accessible(path):
            return ReaderOutcome(locked=True, error="Файл заблокирован")

        settings = load_settings()
        attempted: list[str] = []
        if _find_libreoffice(settings):
            attempted.append("LibreOffice")
            text = self._via_libreoffice(path, settings)
            if text and len(text.strip()) >= 5:
                return self._wrap(text, "libreoffice-second-pass")

        if platform.system() == "Windows":
            attempted.append("MS Word COM")
            text = self._via_com(path)
            if text and len(text.strip()) >= 5:
                return self._wrap(text, "ms-word-com-second-pass")

        if not attempted:
            return ReaderOutcome(
                error=(
                    "Доступные внешние программы для повторного чтения .doc не найдены. "
                    "Укажите LibreOffice в Настройки → Параметры или установите MS Word на Windows"
                ),
                method_used="external-second-pass",
            )
        return ReaderOutcome(
            error="Внешние программы не смогли извлечь текст (" + ", ".join(attempted) + ")",
            method_used="external-second-pass",
        )

    def extract_text(self, path: Path, method: DocReadMethod = DocReadMethod.AUTO) -> ReaderOutcome:
        # Кэшируем настройки один раз — раньше load_settings() вызывалась
        # 2-3 раза на каждый .doc (в _find_libreoffice и _via_libreoffice).
        settings = load_settings()

        # Для OLE2 сначала нужен только заголовок. Ранее весь .doc (до 200 МБ)
        # безусловно читался в память ещё до запуска LibreOffice/Word.
        prefix = self.read_prefix(path, 4096)
        if prefix is None:
            return ReaderOutcome(error="Не удалось прочитать файл")
        if len(prefix) < 8:
            return ReaderOutcome(error="Файл слишком мал / повреждён")
        real_format = doc_formats.detect_format(prefix)

        if real_format == "ZIP":
            # Используем модульный singleton из app.readers вместо создания
            # нового DocxReader() для каждого ZIP-.doc.
            from app.readers import _docx_reader
            outcome = _docx_reader.extract_text(path)
            outcome.method_used = "docx-renamed"
            return outcome

        # data хранит уже прочитанные байты, чтобы не перечитывать файл
        # в _heuristic_fallback при формате UNKNOWN (Change 3).
        data: Optional[bytes] = None

        # Форматам ниже нужны все байты, но настоящие OLE2 читаются полностью
        # только при переходе к эвристическому fallback.
        if real_format in {"RTF", "HTML", "XML", "UNKNOWN"}:
            data = self.read_bytes(path)
            if data is None:
                return ReaderOutcome(error="Не удалось прочитать байты файла")
            if real_format == "RTF":
                return self._wrap(doc_formats.read_rtf(data), "rtf-parser")
            if real_format == "HTML":
                return self._wrap(doc_formats.read_html(data), "html-parser")
            if real_format == "XML":
                return self._wrap(doc_formats.read_word_xml(data), "word2003-xml")
            if method == DocReadMethod.HEURISTIC:
                return self._wrap(doc_formats.read_ole2_heuristic(data), "ole2-heuristic")
            plain_text = doc_formats.read_as_plain_text(data)
            if plain_text:
                return self._wrap(plain_text, "plain-text-fallback")

        if method == DocReadMethod.LIBREOFFICE:
            text = self._via_libreoffice(path, settings)
            if text:
                return self._wrap(text, "libreoffice")
            return self._heuristic_fallback(path, data)
        if method == DocReadMethod.COM:
            text = self._via_com(path)
            if text:
                return self._wrap(text, "ms-word-com")
            return self._heuristic_fallback(path, data)
        if method == DocReadMethod.HEURISTIC:
            return self._heuristic_fallback(path, data)

        # AUTO ориентирован на скорость: сначала дешёвая локальная эвристика,
        # затем точные внешние программы только для файлов, где эвристика не
        # извлекла осмысленный текст. Это резко уменьшает число запусков
        # LibreOffice/Word на больших архивах legacy DOC.
        heuristic = self._heuristic_fallback(path, data)
        if heuristic.text and len(heuristic.text.strip()) >= 5:
            heuristic.method_used = "ole2-heuristic-auto"
            return heuristic

        text = self._via_libreoffice(path, settings)
        if text and len(text.strip()) >= 5:
            return self._wrap(text, "libreoffice-auto-fallback")

        if platform.system() == "Windows":
            text = self._via_com(path)
            if text and len(text.strip()) >= 5:
                return self._wrap(text, "ms-word-com-auto-fallback")

        return ReaderOutcome(
            error=(
                "Не удалось извлечь текст: быстрая эвристика и доступные "
                "внешние программы не дали результата"
            ),
            method_used="auto-all-methods-failed",
        )

    def _heuristic_fallback(self, path: Path, data: Optional[bytes] = None) -> ReaderOutcome:
        if data is None:
            data = self.read_bytes(path)
        if data is None:
            return ReaderOutcome(error="Не удалось прочитать байты файла")
        return self._wrap(doc_formats.read_ole2_heuristic(data), "ole2-heuristic")

    @staticmethod
    def _wrap(text: Optional[str], method: str) -> ReaderOutcome:
        if text and text.strip():
            return ReaderOutcome(text=text, method_used=method)
        return ReaderOutcome(error="Не удалось извлечь текст", method_used=method)

    @staticmethod
    def _via_libreoffice(path: Path, settings=None) -> Optional[str]:
        settings = settings or load_settings()
        soffice = _find_libreoffice(settings)
        if not soffice:
            return None
        timeout = settings.per_file_timeout
        try:
            with tempfile.TemporaryDirectory(prefix="dspscanner-lo-") as tmp_dir:
                tmp_path = Path(tmp_dir)
                profile_dir = tmp_path / "profile"
                profile_dir.mkdir()
                # Отдельный профиль исключает конфликт параллельных soffice и
                # снижает риск выполнения пользовательских макросов/настроек.
                user_installation = profile_dir.resolve().as_uri()
                result = subprocess.run(
                    [
                        soffice,
                        f"-env:UserInstallation={user_installation}",
                        "--headless", "--invisible", "--nologo", "--nodefault",
                        "--norestore", "--nolockcheck", "--nofirststartwizard",
                        "--convert-to", "txt:Text", "--outdir", tmp_dir, str(path),
                    ],
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    return None
                expected = tmp_path / f"{path.stem}.txt"
                candidates = [expected] if expected.is_file() else sorted(tmp_path.glob("*.txt"))
                if not candidates:
                    return None
                return candidates[0].read_text(encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _via_com(path: Path) -> Optional[str]:
        if platform.system() != "Windows":
            return None
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError:
            return None

        pythoncom.CoInitialize()
        word = None
        document = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            try:
                # msoAutomationSecurityForceDisable: не выполнять макросы в
                # документах, открываемых ради извлечения текста.
                word.AutomationSecurity = 3
            except Exception:
                pass
            document = word.Documents.Open(
                str(path.resolve()),
                ConfirmConversions=False,
                ReadOnly=True,
                AddToRecentFiles=False,
                Revert=False,
                NoEncodingDialog=True,
                OpenAndRepair=False,
            )
            return document.Content.Text
        except Exception:
            return None
        finally:
            if document is not None:
                try:
                    document.Close(False)
                except Exception:
                    pass
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
